"""SolidForms Auto-Eject engine, Elegoo Centauri Carbon: turn a 1-plate OrcaSlicer G-code
into one N-unit file that cools, pushes the part off the front of the bed, and starts again.
The planning rules below are shared with the Bambu Lab P1S engine (sf_p1s.py) through `Machine`.

Rules (set by the founder):
- push passes at push_pct (default 70%) of the part height, safety sweep at sweep_pct (10%),
  never lower than MIN_Z; backstop: the push is raised if it would leave more than `clearance` mm above the nozzle
  (proven: the 173 mm dispenser at Z103 = 70 mm clearance);
- purge lines run front-to-back along the left / right bed edges, outside the X band of every
  part, so no push, sweep or falling part ever crosses them.

CLI: python sf_engine.py <in.gcode> [quantity] [out.gcode]
"""
import math
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field, asdict

APP_NAME = "SolidForms Auto-Eject"
VERSION = "3.0"

BED_MAX = 256.0
EDGE = 3.0                   # purge lanes stay this far inside the bed edge
LANE_STEP = 0.9              # X offset between the two passes of a purge lane
PURGE_Y0, PURGE_LEN = 50.0, 150.0
PURGE_GAP = 8.0              # min distance between a purge lane and the parts' X band
PURGE_PITCH_MIN, PURGE_PITCH_MAX = 2.5, 20.0   # lane-to-lane spacing limits (lane is ~1.4 mm wide)
SWEEP_KEEPOUT = 5.0          # sweep lanes stay this far inside the outermost purge lanes
LANE_X_MIN, LANE_X_MAX = 10.0, 236.0   # push/sweep lanes keep off the bed edges and the front-right exclude zone
E_PER_MM = 0.07934           # extrusion rate of the stock CC purge line
MIN_Z = 1.0
STAGE_Y, APPROACH_Y, FINAL_Y = 250.0, 15.0, 5.0
APPROACH_F, FINAL_F = 1500, 600
PARK_X, PARK_Y = 202.0, 264.5
BACK_LIMIT_Y, BACK_WARN_Y = 240.0, 215.0
TEMP_MODE_OVERHEAD_MIN = 15.0  # estimate: cooldown + reheat/home when waiting on temperature
REHEAT_MIN = 5.0


@dataclass(frozen=True)
class Machine:
    """Printer geometry the planner works with (all mm, printer coordinates, Y0 = front)."""
    key: str
    name: str
    match: str                   # lower-case substring of the sliced printer_model
    profile: str                 # what to slice with, for error messages
    bed: float = BED_MAX
    z_max: float = BED_MAX
    edge: float = EDGE
    lane_step: float = LANE_STEP
    purge_y0: float = PURGE_Y0
    purge_len: float = PURGE_LEN
    purge_gap: float = PURGE_GAP
    sweep_keepout: float = SWEEP_KEEPOUT
    lane_x_min: float = LANE_X_MIN
    lane_x_max: float = LANE_X_MAX
    min_z: float = MIN_Z
    stage_y: float = STAGE_Y
    approach_y: float = APPROACH_Y
    final_y: float = FINAL_Y
    park_x: float = PARK_X
    park_y: float = PARK_Y
    back_limit_y: float = BACK_LIMIT_Y
    back_warn_y: float = BACK_WARN_Y
    rod_height: float | None = None   # X-axis rods above the nozzle tip, if the UI should show them
    marks: tuple = ((PARK_X, PARK_Y, "PARK"),)


CC = Machine("cc", "Elegoo Centauri Carbon", "centauri", "the stock Elegoo Centauri Carbon printer profile")
MACHINE = CC


class JobError(Exception):
    pass


@dataclass
class Settings:
    quantity: int = 10
    cool_mode: str = "temp"      # "temp": wait for bed <= cool_temp (M190) | "time": fixed wait
    cool_temp: int = 38
    cool_minutes: float = 15.0
    cool_floor: float = 10.0     # P1S "temp" mode: minimum fan-cooling minutes before the M190
    purge_every: bool = True
    sweep: bool = True
    push_pct: float = 70.0       # push height, % of the part height
    sweep_pct: float = 10.0      # safety sweep height, % of the part height
    purge_pitch: float = 3.5     # spacing between purge lanes, mm
    clearance: float = 70.0
    push_passes: int = 2
    make_zip: bool = True


def rect_hit(a, b):
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def grow(r, m):
    return (r[0] - m, r[1] + m, r[2] - m, r[3] + m)


def union(rects):
    return (min(r[0] for r in rects), max(r[1] for r in rects),
            min(r[2] for r in rects), max(r[3] for r in rects))


def seg_rect(x0, y0, x1, y1):
    return (min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1))


def parse_duration(text):
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(v) * units[u] for v, u in re.findall(r"(\d+)\s*([dhms])", text))


def fmt_min(minutes):
    minutes = int(round(minutes))
    return f"{minutes // 60}h {minutes % 60:02d}m"


MOVE_RE = re.compile(r"([XYZE])(-?\d*\.?\d+)")


def is_move(l):
    return l.startswith("G1 ") or l.startswith("G0 ") or l.startswith("G2 ") or l.startswith("G3 ")


def moves(lines, start, end):
    """Yield (index, x0, y0, x1, y1, extruding) for G0-G3 moves in lines[start:end]."""
    x = y = 0.0
    for i in range(start, end):
        l = lines[i]
        if not is_move(l):
            continue
        nx, ny, ext = x, y, False
        for k, v in MOVE_RE.findall(l.split(";", 1)[0]):
            if k == "X":
                nx = float(v)
            elif k == "Y":
                ny = float(v)
            elif k == "E" and float(v) > 0:
                ext = True
        yield i, x, y, nx, ny, ext
        x, y = nx, ny


ARC_RE = re.compile(r"([IJ])(-?\d*\.?\d+)")


def arc_rect(x0, y0, x1, y1, i, j, cw):
    """Bounding box of a G2 (cw) / G3 arc, including the bulge between its end points."""
    cx, cy, r = x0 + i, y0 + j, math.hypot(i, j)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    sweep = ((a0 - a1) if cw else (a1 - a0)) % (2 * math.pi)
    if sweep < 1e-9:
        sweep = 2 * math.pi                    # end == start: full circle
    xs, ys = [x0, x1], [y0, y1]
    for k in range(4):
        ang = k * math.pi / 2
        if (((a0 - ang) if cw else (ang - a0)) % (2 * math.pi)) <= sweep:
            xs.append(cx + r * math.cos(ang))
            ys.append(cy + r * math.sin(ang))
    return (min(xs), max(xs), min(ys), max(ys))


def ext_moves(lines, start, end):
    """Yield (index, rect, extruding) for G0-G3 moves in lines[start:end]; arcs include their bulge."""
    for (i, x0, y0, x1, y1, ext) in moves(lines, start, end):
        l = lines[i]
        if l.startswith("G2 ") or l.startswith("G3 "):
            ij = dict((k, float(v)) for k, v in ARC_RE.findall(l.split(";", 1)[0]))
            if ij:
                yield i, arc_rect(x0, y0, x1, y1, ij.get("I", 0.0), ij.get("J", 0.0), l.startswith("G2")), ext
                continue
        yield i, seg_rect(x0, y0, x1, y1), ext


class Job:
    """A parsed single-plate OrcaSlicer G-code for the Centauri Carbon."""
    machine = CC

    def __init__(self, path):
        self.path = path
        self.mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            L = [l.rstrip("\r") for l in f.read().split("\n")]
        self.lines = L

        def first(pred, start=0, end=None):
            for i in range(start, len(L) if end is None else end):
                if pred(L[i]):
                    return i
            return None

        self.ex_start = first(lambda l: l.startswith("; EXECUTABLE_BLOCK_START"))
        if self.ex_start is None:
            raise JobError("Not an OrcaSlicer / Elegoo G-code (no EXECUTABLE_BLOCK_START).")
        self.ex_end = first(lambda l: l.startswith("; EXECUTABLE_BLOCK_END"), self.ex_start)
        if self.ex_end is None:
            raise JobError("The file is truncated (no EXECUTABLE_BLOCK_END).")
        self.config_i = first(lambda l: l.startswith("; CONFIG_BLOCK_START"), self.ex_end)
        head = "\n".join(L[:self.ex_start])
        trail = "\n".join(L[self.ex_end:])

        def meta(pattern, text, default=None, cast=str):
            m = re.search(pattern, text, re.M)
            return cast(m.group(1).strip()) if m else default

        self.printer_model = meta(r"^; printer_model = (.+)$", trail, "")
        homes = sum(1 for l in L[self.ex_start:self.ex_end] if re.match(r"G28\b", l))
        if homes > 1:
            raise JobError("This file already holds more than one print. Load the single-plate file "
                           "straight from OrcaSlicer.")

        self.layer0 = first(lambda l: l.startswith(";LAYER:0"), self.ex_start, self.ex_end)
        if self.layer0 is None:
            self.layer0 = first(lambda l: l.startswith(";LAYER_CHANGE"), self.ex_start, self.ex_end)
        if self.layer0 is None:
            raise JobError("No layers found in the file.")
        self.defines = [i for i in range(self.ex_start + 1, self.layer0)
                        if L[i].startswith("EXCLUDE_OBJECT_DEFINE")]
        last_lc = next(i for i in range(self.ex_end, self.layer0 - 1, -1) if L[i].startswith(";LAYER_CHANGE"))
        self.end_i = first(lambda l: l.startswith("; filament end gcode") or l.startswith(";===== CC_END_GCODE"),
                           last_lc, self.ex_end)
        if self.end_i is None:
            raise JobError("Could not find the end G-code (expected the stock Elegoo CC end G-code).")

        self.purge_a = first(lambda l: re.match(r"G1 X-?[\d.]+ Y-1\.2\b", l) is not None, self.ex_start, self.layer0)
        self.purge_b = (first(lambda l: l.startswith(";End PA test."), self.purge_a, self.layer0)
                        if self.purge_a is not None else None)
        self.can_purge = self.purge_a is not None and self.purge_b is not None

        start = "\n".join(L[self.ex_start:self.layer0 + 5])
        self.height = meta(r"^; max_z_height: ([\d.]+)", head, None, float)
        self.layers = (meta(r"^; total layer number: (\d+)", head, None, int)
                       or meta(r"TOTAL_LAYER=(\d+)", start, None, int)
                       or meta(r"^;LAYER_COUNT:(\d+)", start, None, int)
                       or sum(1 for l in L[self.layer0:self.end_i] if l.startswith(";LAYER_CHANGE")))
        t = meta(r"^; estimated printing time \(normal mode\) = (.+)$", trail)
        if t:
            self.unit_min = parse_duration(t) / 60
        else:
            m = next((re.match(r"M73 P\d+ R(\d+)", l) for l in L[self.ex_start:self.layer0]
                      if l.startswith("M73 ")), None)
            self.unit_min = float(m.group(1)) if m else 0.0
        self.filament_g = meta(r"^; total filament used \[g\] = ([\d.]+)", trail, 0.0, float)
        self.filament_type = (meta(r"^; filament_type = (.+)$", trail, "?") or "?").split(";")[0]
        self.skirt_loops = meta(r"^; skirt_loops = (\d+)", trail, 0, int)
        bt = re.search(r"^M190 S(\d+)", start, re.M)
        self.bed_temp = int(bt.group(1)) if bt else None

        self.bed_exclude = []
        ex = meta(r"^; bed_exclude_area = (.+)$", trail)
        if ex:
            pts = [tuple(map(float, p.split("x"))) for p in ex.split(",") if "x" in p]
            if pts:
                self.bed_exclude.append(seg_rect(min(p[0] for p in pts), min(p[1] for p in pts),
                                                 max(p[0] for p in pts), max(p[1] for p in pts)))

        self.objects = []          # (name, rect or None)
        for i in self.defines:
            name = re.search(r"NAME=(\S+)", L[i])
            poly = re.search(r"POLYGON=\[(.*)\]", L[i])
            if not name:
                continue
            rect = None
            if poly:
                pts = [(float(a), float(b)) for a, b in re.findall(r"\[(-?[\d.]+),(-?[\d.]+)\]", poly.group(1))]
                if pts:
                    rect = seg_rect(min(p[0] for p in pts), min(p[1] for p in pts),
                                    max(p[0] for p in pts), max(p[1] for p in pts))
            self.objects.append((name.group(1), rect))

        full, fl, lc, count = None, None, 0, 0
        lc_idx = [i for i in range(self.layer0, self.end_i) if L[i].startswith(";LAYER_CHANGE")]
        for i, r, ext in ext_moves(L, self.layer0, self.end_i):
            while lc < len(lc_idx) and lc_idx[lc] < i:
                lc += 1
            if not ext:
                continue
            count += 1
            full = r if full is None else union([full, r])
            if lc <= 1:
                fl = r if fl is None else union([fl, r])
        if full is None:
            raise JobError("No extrusion found in the print body.")
        self.full_bbox, self.first_bbox, self.ext_count = full, fl or full, count

    @property
    def part_rects(self):
        rects = [r for _, r in self.objects if r]
        return rects + [self.first_bbox] if rects else [self.full_bbox, self.first_bbox]

    def overhead_min(self, s):
        """Estimated minutes each unit adds on top of its print time (cooldown, reheat, homing)."""
        return TEMP_MODE_OVERHEAD_MIN if s.cool_mode == "temp" else s.cool_minutes + REHEAT_MIN

    def check(self, s, p):
        """Printer-specific preflight on top of the shared rules (none for the CC)."""


@dataclass
class Plan:
    push_z: float = 0.0
    push_rule: str = ""
    push_raised: bool = False
    sweep_z: float | None = None
    push_lanes: list = field(default_factory=list)
    sweep_lanes: list = field(default_factory=list)
    purge: list = field(default_factory=list)      # per unit: (x, side) | None
    left_limit: float = 0.0                        # purge lanes must end left of this X
    right_limit: float = BED_MAX                   # ... or start right of this X
    no_purge_xy: tuple = (4.0, 30.0)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    cycle_min: float = 0.0
    total_min: float = 0.0
    total_g: float = 0.0


def spread(a, b, max_gap):
    if b - a <= 1:
        return [round((a + b) / 2, 1)]
    n = math.ceil((b - a) / max_gap) + 1
    return [round(a + (b - a) * k / (n - 1), 1) for k in range(n)]


def lane_rect(x, m=CC):
    return (x - 0.25, x + m.lane_step + 0.25, m.purge_y0 - 0.25, m.purge_y0 + m.purge_len + 0.25)


def make_plan(job, s):
    m = job.machine
    p = Plan(right_limit=m.bed)
    E, W = p.errors, p.warnings
    if m.match not in job.printer_model.lower():
        E.append(f"Sliced for '{job.printer_model or 'unknown printer'}', not the {m.name}.")
    H = job.height
    if H is None:
        E.append("Part height (max_z_height) not found in the header.")
        return p
    if H > m.z_max - 2:
        E.append(f"The part is {H:g} mm tall: there is no room to lift above it.")
    if not 1 <= s.quantity <= 200:
        E.append("Quantity must be between 1 and 200.")
    if s.cool_mode == "temp":
        if job.bed_temp and s.cool_temp >= job.bed_temp:
            E.append(f"Cooldown target {s.cool_temp} °C must be below the print bed temperature ({job.bed_temp} °C).")
        if s.cool_temp < 25:
            E.append("Cooldown targets below 25 °C may never be reached in a warm chamber, and the job would stall.")
    elif not 1 <= s.cool_minutes <= 240:
        E.append("Cooldown time must be between 1 and 240 minutes.")
    ft = job.filament_type.upper()
    if "TPU" in ft or "TPE" in ft:
        E.append("Flexible filament does not release by pushing: auto-eject is not supported for TPU.")
    elif "PETG" in ft:
        W.append("PETG bonds hard to PEI: use a low cooldown target (about 30 °C) and watch the first release.")
    if not job.can_purge:
        E.append("The start G-code's purge line was not recognised, so it cannot be moved to the bed sides. "
                 f"Slice with {m.profile}.")

    parts = job.part_rects
    fp = union(parts)
    if fp[3] > m.back_limit_y:
        E.append(f"The part reaches Y{fp[3]:.0f}. The toolhead parks behind the part at Y{m.stage_y:.0f} before "
                 f"pushing and would land on it. Move it toward the front in the slicer (max Y {m.back_warn_y:.0f}).")
    elif fp[3] > m.back_warn_y:
        W.append(f"The part reaches Y{fp[3]:.0f}: tight for the toolhead parking behind it at Y{m.stage_y:.0f}. "
                 f"Moving it toward the front is safer.")

    if not 10 <= s.push_pct <= 95:
        E.append("Push height must be between 10% and 95% of the part height.")
    if not 1 <= s.sweep_pct <= s.push_pct:
        E.append("Sweep height must be at least 1% and no higher than the push height.")
    if s.purge_every and not PURGE_PITCH_MIN <= s.purge_pitch <= PURGE_PITCH_MAX:
        E.append(f"Purge line spacing must be {PURGE_PITCH_MIN:g}-{PURGE_PITCH_MAX:g} mm.")

    # heights: push_pct / sweep_pct of the part height
    push, sweep = s.push_pct / 100, s.sweep_pct / 100
    p.push_z, p.push_rule = max(push * H, m.min_z), f"{s.push_pct:g}% of part height"
    if H - p.push_z > s.clearance:
        p.push_z, p.push_rule = H - s.clearance, f"raised: part top kept within {s.clearance:g} mm of the nozzle"
        p.push_raised = True
        W.append(f"Very tall part: {s.push_pct:g}% of its height would leave {H - push * H:.0f} mm above the nozzle, "
                 f"more than the {s.clearance:g} mm gantry clearance, so the push runs at Z{p.push_z:.1f} instead.")
    if p.push_z >= 0.9 * H:
        p.push_z, p.push_rule = 0.5 * H, "half height (very thin part)"
    p.push_z = round(p.push_z, 2)
    p.push_lanes = spread(fp[0] + 8, fp[1] - 8, 30) if fp[1] - fp[0] > 20 else [round((fp[0] + fp[1]) / 2, 1)]
    p.push_lanes = sorted({min(max(x, m.lane_x_min), m.lane_x_max) for x in p.push_lanes})

    if s.sweep:
        p.sweep_z = round(min(max(sweep * H, m.min_z), p.push_z), 2)
        if H - p.sweep_z > s.clearance:
            W.append(f"Safety sweep at Z{p.sweep_z:g}: if the push ever fails to clear the part, the standing part "
                     f"will hit the gantry when the bed rises for the sweep. Watch the first cycle.")

    # purge lanes along the bed sides, outside the parts' X band
    p.left_limit = fp[0] - m.purge_gap
    p.right_limit = fp[1] + m.purge_gap
    excl = [grow(r, 2) for r in job.bed_exclude]
    pitch = min(max(s.purge_pitch, PURGE_PITCH_MIN), PURGE_PITCH_MAX)
    left, right = [], []
    x = m.edge
    while x + m.lane_step + 0.25 <= p.left_limit:
        if not any(rect_hit(lane_rect(x, m), e) for e in excl):
            left.append(round(x, 2))
        x += pitch
    x = m.bed - m.edge - m.lane_step
    while x - 0.25 >= p.right_limit:
        if not any(rect_hit(lane_rect(x, m), e) for e in excl):
            right.append(round(x, 2))
        x -= pitch
    slots = []
    for i in range(max(len(left), len(right))):
        if i < len(left):
            slots.append((left[i], "L"))
        if i < len(right):
            slots.append((right[i], "R"))
    need = s.quantity if s.purge_every else 1
    if job.can_purge and len(slots) < need:
        E.append(f"Only {len(slots)} purge lanes fit beside the part (need {need}). Center the part in the slicer "
                 f"to free both bed sides, or purge before the first unit only.")
    p.purge = [slots[u] if u < len(slots) and (s.purge_every or u == 0) else None for u in range(s.quantity)]
    used = [lane for lane in p.purge if lane]
    if not s.purge_every:
        if left and left[0] + 4 <= p.left_limit:
            p.no_purge_xy = (m.edge + 1, m.purge_y0 - 20)
        elif right:
            p.no_purge_xy = (m.bed - m.edge - 1, m.purge_y0 - 20)
        else:
            p.no_purge_xy = ((fp[0] + fp[1]) / 2, max(5.0, fp[2] - 20))
        if job.skirt_loops == 0:
            W.append("Units 2+ start without a purge line. Enable a 1-loop skirt in the slicer so the nozzle primes "
                     "before the first perimeter.")

    lx = max([x for x, side in used if side == "L"], default=None)
    rx = min([x for x, side in used if side == "R"], default=None)
    a = max((lx + m.lane_step + m.sweep_keepout) if lx is not None else 0.0, m.lane_x_min)
    b = min((rx - m.sweep_keepout) if rx is not None else m.bed, m.lane_x_max)
    if p.sweep_z is not None:
        p.sweep_lanes = spread(a, b, 36)

    job.check(s, p)
    p.cycle_min = job.unit_min + job.overhead_min(s)
    p.total_min = p.cycle_min * s.quantity
    p.total_g = job.filament_g * s.quantity
    return p


def lane_gcode(unit, x, side):
    e = round(PURGE_LEN * E_PER_MM, 3)
    y0, y1 = PURGE_Y0, PURGE_Y0 + PURGE_LEN
    return [
        f"; --- purge lane, unit {unit}: {'left' if side == 'L' else 'right'} side X{x:.2f}, Y{y0:.0f}-{y1:.0f} ---",
        f"G1 X{x:.2f} Y{y0:.2f} F20000",
        "G1 Z0.3 F900",
        "G90",
        "M83",
        "G92 E0 ;Reset Extruder",
        "G1 F6000",
        f"G1 Y{y1:.2f} E{e}",
        f"G1 X{x + LANE_STEP:.2f} E0.1",
        "G1 F3000",
        f"G1 Y{y0:.2f} E{e}",
        ";End purge lane.",
    ]


def no_purge_gcode(unit, xy):
    return [
        f"; --- unit {unit}: no purge line (purge before first unit only) ---",
        f"G1 X{xy[0]:.2f} Y{xy[1]:.2f} F20000",
        "G1 Z2 F900",
        "G90",
        "M83",
        "G92 E0 ;Reset Extruder",
    ]


def eject_gcode(job, s, p, unit, last):
    H = job.height
    lift = min(BED_MAX, H + 2)
    park = min(BED_MAX, max(233.0, H + 2))
    wait = (f"cool to {s.cool_temp}C" if s.cool_mode == "temp" else f"cool {s.cool_minutes:g} min")
    out = [
        f";===== SF AUTO-EJECT unit {unit}/{s.quantity}: {wait}, push Z{p.push_z:g}"
        f"{f', sweep Z{p.sweep_z:g}' if p.sweep_z is not None else ''} =====",
        "M400",
        "M83",
        "G92 E0",
        "G1 E-0.8 F1800 ; stop ooze",
        "G90",
        f"G1 Z{lift:.2f} F1200 ; lift clear of the top surface",
        f"G1 X{PARK_X:.0f} F20000",
        f"G1 Y{STAGE_Y:.0f} F20000",
        f"G1 Y{PARK_Y} F1200 ; park at the back",
        "M400",
        "M104 S0 ; hotend off",
        "M140 S0 ; bed heater off",
        "M106 S255 ; part fan max",
        "M106 P2 S255 ; aux fan max",
        "M106 P3 S200 ; chamber fan",
    ]
    if s.cool_mode == "temp":
        out.append(f"M190 S{s.cool_temp} ; wait until the bed is at {s.cool_temp}C so the part releases")
    else:
        out.append(f"G4 P{int(s.cool_minutes * 60000)} ; cool for {s.cool_minutes:g} min, then push regardless")
    out += ["M400", f"; --- push at Z{p.push_z:g} ({p.push_rule}): back -> front ---", f"G1 Z{p.push_z:.2f} F1200", "M400"]

    def lane(x):
        return [f"G1 X{x} Y{STAGE_Y:.0f} F12000",
                f"G1 X{x} Y{APPROACH_Y:.0f} F{APPROACH_F}",
                f"G1 X{x} Y{FINAL_Y:.0f} F{FINAL_F}",
                "M400",
                f"G1 X{x} Y{STAGE_Y:.0f} F12000"]

    for n in range(s.push_passes):
        out.append(f"; push pass {n + 1}/{s.push_passes}")
        for x in p.push_lanes:
            out += lane(x)
    if p.sweep_z is not None:
        out += [f"; --- safety sweep at Z{p.sweep_z:g} ({s.sweep_pct:g}% of part height) ---",
                f"G1 X{p.sweep_lanes[0]} Y{STAGE_Y:.0f} F12000", f"G1 Z{p.sweep_z:.2f} F1200", "M400"]
        for x in p.sweep_lanes:
            out += lane(x)
    out += ["M106 S0", "M106 P2 S0", "M106 P3 S0", f"G1 Z{park:.0f} F3000", "G1 X5 Y255 F20000", "M400"]
    if last:
        out += [f";===== JOB COMPLETE: {s.quantity} units =====",
                "M104 S0 ;Turn-off hotend", "M140 S0 ;Turn-off bed",
                "M106 S0", "M106 P2 S0", "M106 P3 S0", "M84 ;Disable all steppers", "M73 P100 R0"]
    else:
        out.append(f";===== end of unit {unit}, starting unit {unit + 1} =====")
    return out


def write_output(job, s, p, out_path, progress=None):
    L, Q = job.lines, s.quantity
    dset = set(job.defines)
    names = {n for n, _ in job.objects}
    name_re = re.compile(r"NAME=(\S+)")
    m73_re = re.compile(r"M73 P(\d+) R(\d+)")
    total_re = re.compile(r"TOTAL_LAYER=\d+")
    current_re = re.compile(r"CURRENT_LAYER=(\d+)")
    total_layers = job.layers * Q
    overhead = p.cycle_min - job.unit_min

    def rename(l, u):
        return name_re.sub(lambda m: f"NAME={m.group(1)}_u{u + 1:02d}" if m.group(1) in names else m.group(0), l)

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        w = f.write
        for l in L[:job.ex_start]:
            w(re.sub(r"^; total layer number: \d+", f"; total layer number: {total_layers}", l) + "\n")
        w(L[job.ex_start] + "\n")
        w(f"; {APP_NAME} v{VERSION}: {Q} units, "
          f"{'cool to %dC' % s.cool_temp if s.cool_mode == 'temp' else 'cool %g min' % s.cool_minutes}, "
          f"push Z{p.push_z:g} lanes {p.push_lanes}, sweep Z{p.sweep_z}, "
          f"purge {'every unit' if s.purge_every else 'first unit only'} (bed sides)\n")
        for u in range(Q):
            for i in job.defines:
                w(rename(L[i], u) + "\n")
        for u in range(Q):
            w(f";===== SF UNIT {u + 1}/{Q} =====\n")
            lane = p.purge[u]
            for i in range(job.ex_start + 1, job.end_i):
                if i in dset:
                    continue
                if job.purge_a <= i <= job.purge_b:
                    if i == job.purge_a:
                        g = lane_gcode(u + 1, *lane) if lane else no_purge_gcode(u + 1, p.no_purge_xy)
                        w("\n".join(g) + "\n")
                    continue
                l = L[i]
                if l.startswith("M73 "):
                    m = m73_re.match(l)
                    if m:
                        rem = int(m.group(2)) + overhead + (Q - 1 - u) * p.cycle_min
                        pct = max(0, min(99, round(100 * (1 - rem / p.total_min)))) if p.total_min else 0
                        l = f"M73 P{pct} R{int(round(rem))}"
                elif l.startswith("SET_PRINT_STATS_INFO"):
                    l = total_re.sub(f"TOTAL_LAYER={total_layers}", l)
                    l = current_re.sub(lambda m: f"CURRENT_LAYER={int(m.group(1)) + u * job.layers}", l)
                elif l.startswith(";LAYER_COUNT:"):
                    l = f";LAYER_COUNT:{total_layers}"
                elif l.startswith("EXCLUDE_OBJECT"):
                    l = rename(l, u)
                w(l + "\n")
            w("\n".join(eject_gcode(job, s, p, u + 1, u == Q - 1)) + "\n")
            if progress:
                progress(u + 1, Q)
        w(L[job.ex_end] + "\n")
        stop = job.config_i if job.config_i is not None else len(L)
        for l in L[job.ex_end + 1:stop]:
            if re.match(r"; (total )?filament (used|cost)", l):
                l = re.sub(r"(= )([\d.]+(?:, [\d.]+)*)",
                           lambda m: m.group(1) + ", ".join(f"{float(v) * Q:.2f}" for v in m.group(2).split(", ")), l)
            elif l.startswith("; total layers count ="):
                l = f"; total layers count = {total_layers}"
            elif l.startswith("; estimated printing time"):
                l = f"; estimated printing time (normal mode) = {int(p.total_min // 60)}h {int(p.total_min % 60)}m 0s"
            w(l + "\n")
        if job.config_i is not None:
            w("\n".join(L[job.config_i:]))


def validate(job, s, p, out_path):
    """Re-read the written file and check it. Returns a list of (ok, label, detail)."""
    Q = s.quantity
    parts = job.part_rects
    unit, mode = 0, None
    x = y = z = 0.0
    purge, print_ext = {}, {}
    m73, layers, oob = [], [], []
    g28 = cools = eject_ext = 0
    layer0_tag = job.lines[job.layer0]
    cool_cmd = f"M190 S{s.cool_temp}" if s.cool_mode == "temp" else f"G4 P{int(s.cool_minutes * 60000)}"
    with open(out_path, encoding="utf-8") as f:
        for n, l in enumerate(f, 1):
            l = l.rstrip("\n")
            if l.startswith(";===== SF UNIT "):
                unit, mode = int(l.split()[3].split("/")[0]), "start"
                continue
            if mode == "start" and l == layer0_tag:
                mode = "print"
            elif l.startswith(";===== SF AUTO-EJECT"):
                mode = "eject"
            if l.startswith("G28"):
                g28 += 1
            elif l.startswith(cool_cmd):
                cools += 1
            elif l.startswith("M73 "):
                m = re.match(r"M73 P(\d+) R(\d+)", l)
                if m:
                    m73.append((int(m.group(1)), int(m.group(2))))
            elif l.startswith("SET_PRINT_STATS_INFO"):
                m = re.search(r"CURRENT_LAYER=(\d+)", l)
                if m:
                    layers.append(int(m.group(1)))
            if not is_move(l):
                continue
            nx, ny, nz, ext = x, y, z, False
            for k, v in MOVE_RE.findall(l.split(";", 1)[0]):
                v = float(v)
                if k == "X":
                    nx = v
                elif k == "Y":
                    ny = v
                elif k == "Z":
                    nz = v
                elif k == "E" and v > 0:
                    ext = True
            if ext:
                if mode == "start":
                    purge.setdefault(unit, []).append(seg_rect(x, y, nx, ny))
                elif mode == "print":
                    print_ext[unit] = print_ext.get(unit, 0) + 1
                elif mode == "eject":
                    eject_ext += 1
            if not (-2 <= nx <= BED_MAX and -2 <= ny <= 265 and 0 <= nz <= BED_MAX):
                oob.append(n)
            x, y, z = nx, ny, nz

    checks = []

    def check(ok, label, detail):
        checks.append((bool(ok), label, detail))

    bad = [u for u in range(1, Q + 1) if print_ext.get(u, 0) != job.ext_count]
    check(not bad, "Print moves identical to the source",
          f"{job.ext_count:,} moves × {Q} units" if not bad else f"units {bad} differ")
    check(g28 == Q and cools == Q, "Every unit homes, and cools before the push",
          f"{g28} homes, {cools} cooldowns for {Q} units")
    check(eject_ext == 0, "No extrusion during ejection", f"{eject_ext} extrusion moves found" if eject_ext else "clean")
    check(not oob, "All moves inside the machine", f"lines {oob[:5]}" if oob else "X 0-256 · Y 0-265 · Z 0-256")
    segs = [(u, r) for u, rs in purge.items() for r in rs]
    hit_part = sorted({u for u, r in segs if any(rect_hit(grow(r, 0.3), q) for q in parts)})
    in_band = sorted({u for u, r in segs if not (r[1] <= p.left_limit or r[0] >= p.right_limit)})
    check(not hit_part and not in_band, "Purge lines on the bed sides, clear of every part",
          f"{len(purge)} purge lanes" if not (hit_part or in_band) else f"units {sorted(set(hit_part + in_band))}")
    overlap = []
    us = sorted(purge)
    for i, a in enumerate(us):
        for b in us[i + 1:]:
            if any(rect_hit(grow(r1, 0.3), grow(r2, 0.3)) for r1 in purge[a] for r2 in purge[b]):
                overlap.append((a, b))
    check(not overlap, "No purge line overlaps another", f"{overlap[:3]}" if overlap else "each unit has its own lane")
    lanes_x = p.push_lanes + (p.sweep_lanes if p.sweep_z is not None else [])
    crossing = sorted({u for u, r in segs for lx in lanes_x if r[0] - 3 < lx < r[1] + 3})
    check(not crossing, "No push or sweep crosses a purge line",
          f"units {crossing}" if crossing else f"{len(lanes_x)} lanes checked")
    m73_ok = all(a[0] <= b[0] and a[1] >= b[1] for a, b in zip(m73, m73[1:]))
    lay_ok = not layers or (all(a <= b for a, b in zip(layers, layers[1:]))
                            and job.layers * (Q - 1) < max(layers) <= job.layers * Q)
    check(m73_ok and lay_ok, "Progress and layer counter run through the whole job",
          f"{job.layers * Q:,} layers" if lay_ok else "layer counter breaks")
    return checks


def output_name(path, s):
    stem = os.path.splitext(os.path.basename(path))[0]
    return f"{stem}_x{s.quantity}_AutoEject.gcode"


def build(job, s, out_path, progress=None):
    p = make_plan(job, s)
    if p.errors:
        return p, None, None
    write_output(job, s, p, out_path, progress)
    checks = validate(job, s, p, out_path)
    zip_path = None
    if s.make_zip and all(ok for ok, _, _ in checks):
        zip_path = os.path.splitext(out_path)[0] + ".zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.write(out_path, os.path.basename(out_path))
    return p, checks, zip_path


def plan_dict(job, s, p):
    """Everything the UI draws, as plain JSON."""
    fp = union(job.part_rects)
    m = job.machine
    return {
        "file": os.path.basename(job.path),
        "path": job.path,
        "printer": job.printer_model,
        "machine": {"key": m.key, "name": m.name, "bed": m.bed, "z_max": m.z_max, "rod_height": m.rod_height,
                    "marks": [list(k) for k in m.marks]},
        "package": "3mf" if getattr(job, "is_3mf", False) else "gcode",
        "cool_floor": s.cool_floor,
        "material": job.filament_type,
        "bed_temp": job.bed_temp,
        "height": job.height,
        "footprint": [round(fp[1] - fp[0], 1), round(fp[3] - fp[2], 1)],
        "objects": len(job.objects) or 1,
        "unit_min": round(job.unit_min, 1),
        "unit_g": job.filament_g,
        "layers": job.layers,
        "parts": [list(map(lambda v: round(v, 2), r)) for r in job.part_rects[:-1] or job.part_rects],
        "exclude": [list(r) for r in job.bed_exclude],
        "push_z": p.push_z,
        "push_rule": p.push_rule,
        "push_raised": p.push_raised,
        "push_pct": s.push_pct,
        "sweep_pct": s.sweep_pct,
        "purge_pitch": s.purge_pitch,
        "sweep_z": p.sweep_z,
        "push_lanes": p.push_lanes,
        "sweep_lanes": p.sweep_lanes,
        "purge": [list(l) if l else None for l in p.purge],
        "purge_y": [m.purge_y0, m.purge_y0 + m.purge_len],
        "lane_step": m.lane_step,
        "left_limit": p.left_limit,
        "right_limit": p.right_limit,
        "stage_y": m.stage_y,
        "final_y": m.final_y,
        "clearance": s.clearance,
        "total_min": round(p.total_min, 1),
        "cycle_min": round(p.cycle_min, 1),
        "total_g": round(p.total_g, 1),
        "errors": p.errors,
        "warnings": p.warnings,
        "output_name": job.engine.output_name(job.path, s),
    }


def settings_from(d):
    base = asdict(Settings())
    base.update({k: d[k] for k in base if k in d and d[k] is not None})
    s = Settings(**base)
    s.quantity, s.cool_temp, s.push_passes = int(s.quantity), int(s.cool_temp), int(s.push_passes)
    s.cool_minutes, s.cool_floor, s.clearance = float(s.cool_minutes), float(s.cool_floor), float(s.clearance)
    s.push_pct, s.sweep_pct, s.purge_pitch = float(s.push_pct), float(s.sweep_pct), float(s.purge_pitch)
    return s


Job.engine = sys.modules[__name__]


if __name__ == "__main__":
    src = sys.argv[1]
    s = Settings(quantity=int(sys.argv[2]) if len(sys.argv) > 2 else 10)
    job = Job(src)
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(src), output_name(src, s))
    p, checks, zp = build(job, s, out)
    d = plan_dict(job, s, p)
    print({k: d[k] for k in ("push_z", "push_rule", "sweep_z", "push_lanes", "sweep_lanes", "purge", "errors", "warnings")})
    for ok, label, detail in checks or []:
        print("OK  " if ok else "FAIL", label, "-", detail)
    sys.exit(1 if p.errors or not checks or not all(c[0] for c in checks) else 0)
