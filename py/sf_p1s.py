"""SolidForms Auto-Eject engine, Bambu Lab P1S: turn a 1-plate OrcaSlicer / Bambu Studio G-code
(.gcode, or a .gcode.3mf "plate sliced file") into one N-unit job that cools, pushes the part off the
front of the bed, and starts again.

The planning rules (push / sweep heights, side purge lanes, ejection band) are shared with the CC
engine through sf_engine.make_plan and the P1S `Machine` below. What is P1S-specific:
- every unit re-runs the stock Bambu start G-code (heat, home, nozzle wipe, bed levelling, vibration
  check); only its front "nozzle load line" is swapped for a side purge lane;
- filament stays loaded between units: the stock end G-code (filament back to the AMS, bed down)
  runs once, after the last unit;
- cooldown "temp" mode = a guaranteed fan-cooling time, THEN M190 to the target. Bambu does not
  document whether M190 waits for a bed that is cooling, so the time floor is the guarantee;
- pushes stay right of X30 near the front: the filament cutter lever is at X20 Y-3;
- a .gcode.3mf is written back as a .gcode.3mf with the new G-code, its MD5 and slice_info updated.

CLI: python sf_p1s.py <in.gcode|in.gcode.3mf> [quantity] [out]
"""
import hashlib
import os
import re
import sys
import zipfile

import sf_engine as cc
from sf_engine import (APP_NAME, VERSION, JobError, Machine, MOVE_RE, ext_moves, grow, is_move, make_plan,
                       parse_duration, rect_hit, seg_rect, settings_from, union)

MACHINE = P1S = Machine(
    "p1s", "Bambu Lab P1S", "p1s", "the stock Bambu Lab P1S printer profile (OrcaSlicer or Bambu Studio)",
    z_max=250.0, lane_x_min=30.0, stage_y=254.0, final_y=2.0, park_x=65.0, park_y=265.0,
    back_limit_y=224.0, back_warn_y=210.0, rod_height=34.0,
    marks=((65.0, 265.0, "PARK · CHUTE"), (20.0, -3.0, "CUTTER")))
APPROACH_F, FINAL_F = cc.APPROACH_F, cc.FINAL_F
EJECT_ACCEL = 2000             # M204 during pushes; the next unit's start G-code restores the stock value
TEMP_EXTRA_MIN = 5.0           # estimate on top of the floor: M190 tail + reheat
REHEAT_MIN = 5.0
COOL_FLOOR_MAX = 120
LOAD_E_PER_MM, LOAD_F = 15 / 222, 6000   # stock P1S load line, used if the file's own can't be read
PLATE_RE = re.compile(r"^Metadata/plate_\d+\.gcode$")
SLICE_INFO = "Metadata/slice_info.config"


def read_source(path):
    """Return (G-code text, plate entry name inside the .3mf or None)."""
    if not path.lower().endswith(".3mf"):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), None
    try:
        with zipfile.ZipFile(path) as z:
            plates = [n for n in z.namelist() if PLATE_RE.match(n)]
            if not plates:
                raise JobError("This .3mf holds no sliced plate. Slice it, then use File > Export > "
                               "Export plate sliced file (or export plain G-code).")
            if len(plates) > 1:
                raise JobError(f"This .3mf holds {len(plates)} sliced plates. Export one plate "
                               "(File > Export > Export plate sliced file).")
            return z.read(plates[0]).decode("utf-8", "replace"), plates[0]
    except zipfile.BadZipFile:
        raise JobError("This .3mf is damaged or is not a sliced project.")


class Job:
    """A parsed single-plate Bambu Lab P1S G-code (OrcaSlicer or Bambu Studio)."""
    machine = P1S

    def __init__(self, path):
        self.path = path
        self.mtime = os.path.getmtime(path)
        self.is_3mf = path.lower().endswith(".3mf")
        text, self.plate = read_source(path)
        L = [l.rstrip("\r") for l in text.split("\n")]
        self.lines = L

        def first(pred, start=0, end=None):
            for i in range(start, len(L) if end is None else end):
                if pred(L[i]):
                    return i
            return None

        self.ex_start = first(lambda l: l.startswith("; EXECUTABLE_BLOCK_START"))
        if self.ex_start is None:
            raise JobError("Not a Bambu / OrcaSlicer G-code (no EXECUTABLE_BLOCK_START).")
        self.ex_end = first(lambda l: l.startswith("; EXECUTABLE_BLOCK_END"), self.ex_start)
        if self.ex_end is None:
            raise JobError("The file is truncated (no EXECUTABLE_BLOCK_END).")
        head = "\n".join(L[:self.ex_start])
        trail = "\n".join(L[self.ex_end:])

        def meta(pattern, default=None, cast=str):
            for text in (head, trail):
                m = re.search(pattern, text, re.M)
                if m:
                    return cast(m.group(1).strip())
            return default

        self.printer_model = meta(r"^; printer_model = (.+)$", "")
        if "centauri" in self.printer_model.lower():
            raise JobError("Centauri Carbon jobs go in as plain G-code: export G-code from OrcaSlicer, not a .3mf.")
        if P1S.match not in self.printer_model.lower():
            raise JobError(f"Sliced for '{self.printer_model or 'an unknown printer'}'. This app supports the "
                           "Elegoo Centauri Carbon (.gcode) and the Bambu Lab P1S (.gcode or .gcode.3mf).")
        if L[self.ex_start + 1].startswith("; " + APP_NAME):
            raise JobError("This is already an Auto-Eject batch file. Load the single-plate file from the slicer.")
        starts = [i for i in range(self.ex_start, self.ex_end) if L[i].startswith(";===== machine:")]
        if len(starts) > 1:
            raise JobError("This file already holds more than one print. Load the single-plate file "
                           "straight from the slicer.")
        if not starts:
            raise JobError(f"The stock Bambu start G-code was not found. Slice with {P1S.profile}.")

        self.layer0 = first(lambda l: l.startswith("; CHANGE_LAYER"), self.ex_start, self.ex_end)
        if self.layer0 is None:
            raise JobError("No layers found in the file.")
        lc_idx = [i for i in range(self.layer0, self.ex_end) if L[i].startswith("; CHANGE_LAYER")]
        self.end_i = first(lambda l: l.startswith("; MACHINE_END_GCODE_START") or l.startswith("; filament end gcode"),
                           lc_idx[-1], self.ex_end)
        if self.end_i is None:
            raise JobError("Could not find the end G-code (expected the stock Bambu P1S end G-code).")
        self.end_tag = L[self.end_i]
        self.end_tag_in_body = sum(1 for l in L[self.layer0:self.end_i] if l == self.end_tag)

        # the stock front "nozzle load line" -> replaced by a side lane
        self.purge_a = first(lambda l: l.startswith(";===== nozzle load line"), self.ex_start, self.layer0)
        self.purge_b = (first(lambda l: l.startswith("M400"), self.purge_a, self.layer0)
                        if self.purge_a is not None else None)
        self.load_temp = None
        self.load_e_per_mm, self.load_f = LOAD_E_PER_MM, LOAD_F
        if self.purge_b is not None:
            block = L[self.purge_a:self.purge_b]
            self.load_temp = next((l.split(";")[0].strip() for l in block if l.startswith("M109 S")), None)
            x = None
            for l in block:
                vals = dict(MOVE_RE.findall(l.split(";", 1)[0]))
                f = re.search(r"\bF([\d.]+)", l)
                if is_move(l) and "X" in vals and "E" in vals and x is not None and abs(float(vals["X"]) - x) > 50:
                    self.load_e_per_mm = float(vals["E"]) / abs(float(vals["X"]) - x)
                    self.load_f = float(f.group(1)) if f else LOAD_F
                    break
                if is_move(l) and "X" in vals:
                    x = float(vals["X"])
        self.can_purge = self.load_temp is not None

        self.height = meta(r"^; max_z_height: ([\d.]+)", None, float)
        self.layers = meta(r"^; total layer number: (\d+)", None, int) or len(lc_idx)
        t = meta(r"total estimated time: ([^;\n]+)")
        if t:
            self.unit_min = parse_duration(t) / 60
        else:
            m = next((re.match(r"M73 P\d+ R(\d+)", l) for l in L[self.ex_start:self.layer0] if l.startswith("M73 P")),
                     None)
            self.unit_min = float(m.group(1)) if m else 0.0
        used = meta(r"^; filament used \[g\] = (.+)$")
        self.filament_g = (sum(float(v) for v in re.findall(r"[\d.]+", used)) if used
                           else meta(r"^; total filament weight \[g\] : ([\d.]+)", 0.0, float))
        self.filament_type = (meta(r"^; filament_type = (.+)$", "?") or "?").split(";")[0]
        self.skirt_loops = meta(r"^; skirt_loops = (\d+)", 0, int)
        bt = re.search(r"^M190 S(\d+)", "\n".join(L[self.ex_start:self.layer0]), re.M)
        self.bed_temp = int(bt.group(1)) if bt else None
        self.g28_per_unit = sum(1 for l in L[self.ex_start:self.end_i] if re.match(r"G28\b", l))

        self.bed_exclude = []
        ex = meta(r"^; bed_exclude_area = (.+)$")
        if ex:
            pts = [tuple(map(float, p.split("x"))) for p in ex.split(",") if "x" in p]
            if pts:
                self.bed_exclude.append(seg_rect(min(p[0] for p in pts), min(p[1] for p in pts),
                                                 max(p[0] for p in pts), max(p[1] for p in pts)))

        # per-object footprints from the label markers (Orca: start/stop printing object, Studio: OBJECT_ID)
        switches = []
        for i in range(self.layer0, self.end_i):
            l = L[i]
            if l.startswith("; start printing object, unique label id:") or l.startswith("; OBJECT_ID:"):
                switches.append((i, l.rsplit(":", 1)[1].strip()))
            elif l.startswith("; stop printing object, unique label id:"):
                switches.append((i, None))
        rects, other = {}, None
        full, fl, lc, sw, label, count = None, None, 0, 0, None, 0
        for i, r, ext in ext_moves(L, self.layer0, self.end_i):
            while lc < len(lc_idx) and lc_idx[lc] < i:
                lc += 1
            while sw < len(switches) and switches[sw][0] < i:
                label = switches[sw][1]
                sw += 1
            if not ext:
                continue
            count += 1
            full = r if full is None else union([full, r])
            if lc <= 1:
                fl = r if fl is None else union([fl, r])
            if label is None:
                other = r if other is None else union([other, r])
            else:
                rects[label] = r if label not in rects else union([rects[label], r])
        if full is None:
            raise JobError("No extrusion found in the print body.")
        self.full_bbox, self.first_bbox, self.ext_count = full, fl or full, count
        self.objects = [(f"object {k}", v) for k, v in rects.items()]
        self.other_bbox = other

    @property
    def part_rects(self):
        rects = [r for _, r in self.objects]
        if not rects:
            return [self.full_bbox, self.first_bbox]
        return rects + ([self.other_bbox] if self.other_bbox else []) + [self.first_bbox]

    def overhead_min(self, s):
        return s.cool_floor + TEMP_EXTRA_MIN if s.cool_mode == "temp" else s.cool_minutes + REHEAT_MIN

    def check(self, s, p):
        E, W = p.errors, p.warnings
        if s.cool_mode == "temp":
            if not 0 <= s.cool_floor <= COOL_FLOOR_MAX:
                E.append(f"Minimum cooling time must be 0-{COOL_FLOOR_MAX} minutes.")
            elif s.cool_floor < 5:
                W.append("Under 5 min of guaranteed cooling: if the P1S's M190 does not wait for a cooling bed, "
                         "the push meets a part that is still warm and stuck.")
        if self.height and p.push_z and self.height - p.push_z > P1S.rod_height:
            W.append(f"During the push the part top stands {self.height - p.push_z:.0f} mm above the nozzle, higher "
                     f"than the X-axis rods ({P1S.rod_height:g} mm). The toolhead front still does the pushing; "
                     "watch the first ejection.")


def lane_gcode(job, unit, x, side):
    m = P1S
    y0, y1 = m.purge_y0, m.purge_y0 + m.purge_len
    e, f = round(m.purge_len * job.load_e_per_mm, 3), round(job.load_f, 2)
    return [
        f"; --- purge lane, unit {unit}: {'left' if side == 'L' else 'right'} side X{x:.2f}, Y{y0:.0f}-{y1:.0f} "
        f"(replaces the stock front load line) ---",
        "M975 S1",
        "G90",
        "M83",
        "T1000",
        f"G1 X{x:.2f} Y{y0:.2f} Z0.8 F18000",
        f"{job.load_temp} ; wait for nozzle temperature",
        "G1 Z0.2",
        "G1 E2 F300",
        f"G1 Y{y1:.2f} E{e} F{f}",
        f"G1 X{x + m.lane_step:.2f} E0.07 F{round(f / 4, 2)}",
        f"G1 Y{y0:.2f} E{e} F{f}",
        "M400",
        ";End purge lane.",
    ]


def no_purge_gcode(job, unit, xy):
    return [
        f"; --- unit {unit}: no purge line (purge before first unit only) ---",
        "M975 S1",
        "G90",
        "M83",
        f"G1 X{xy[0]:.2f} Y{xy[1]:.2f} Z2 F18000",
        f"{job.load_temp} ; wait for nozzle temperature",
        ";End no purge.",
    ]


def wait_minutes(minutes, why):
    whole, sec = int(minutes), int(round((minutes - int(minutes)) * 60))
    out = [f"; {why}"] + [f"M400 S60 ; cooling, minute {k + 1}/{whole + (1 if sec else 0)}" for k in range(whole)]
    if sec:
        out.append(f"M400 S{sec}")
    return out


def eject_gcode(job, s, p, unit, last):
    m, H = P1S, job.height
    lift = min(m.z_max - 2, H + 3)
    rest = min(m.z_max - 2, max(p.push_z, p.sweep_z or 0) + 10)
    wait = (f"cool {s.cool_floor:g} min + to {s.cool_temp}C" if s.cool_mode == "temp" else f"cool {s.cool_minutes:g} min")
    out = [
        f";===== SF AUTO-EJECT unit {unit}/{s.quantity}: {wait}, push Z{p.push_z:g}"
        f"{f', sweep Z{p.sweep_z:g}' if p.sweep_z is not None else ''} =====",
        "M400",
        "M83",
        "G92 E0",
        "G1 E-0.8 F1800 ; stop ooze",
        "G90",
        f"G1 Z{lift:.2f} F900 ; bed down, clear of the part top",
        f"G1 X{m.park_x:.0f} Y245 F12000 ; stock end-G-code route to the back",
        f"G1 Y{m.park_y:.0f} F3000 ; park above the purge chute",
        "M400",
        "M104 S0 ; hotend off",
        "M140 S0 ; bed heater off",
        "M106 S255 ; part fan max",
        "M106 P2 S255 ; aux fan max",
        "M106 P3 S255 ; chamber fan max",
    ]
    if s.cool_mode == "temp":
        out += wait_minutes(s.cool_floor, f"guaranteed cooling: {s.cool_floor:g} min with fans at max")
        out += [f"M190 S{s.cool_temp} ; then wait for the bed to read {s.cool_temp}C",
                "M140 S0 ; clear the M190 target: heater stays off"]
    else:
        out += wait_minutes(s.cool_minutes, f"cool {s.cool_minutes:g} min with fans at max, then push regardless")
    out += ["M400", f"M204 S{EJECT_ACCEL} ; gentle accelerations for pushing",
            f"; --- push at Z{p.push_z:g} ({p.push_rule}): back -> front ---",
            f"G1 X{p.push_lanes[0]} Y{m.stage_y:.0f} F12000 ; behind the part, above it",
            f"G1 Z{p.push_z:.2f} F1200", "M400"]

    def lane(x):
        return [f"G1 X{x} Y{m.stage_y:.0f} F12000",
                f"G1 X{x} Y{m.approach_y:.0f} F{APPROACH_F}",
                f"G1 X{x} Y{m.final_y:.0f} F{FINAL_F}",
                "M400",
                f"G1 X{x} Y{m.stage_y:.0f} F12000"]

    for n in range(s.push_passes):
        out.append(f"; push pass {n + 1}/{s.push_passes}")
        for x in p.push_lanes:
            out += lane(x)
    if p.sweep_z is not None:
        out += [f"; --- safety sweep at Z{p.sweep_z:g} ({s.sweep_pct:g}% of part height) ---",
                f"G1 X{p.sweep_lanes[0]} Y{m.stage_y:.0f} F12000", f"G1 Z{p.sweep_z:.2f} F1200", "M400"]
        for x in p.sweep_lanes:
            out += lane(x)
    out += ["M106 S0", "M106 P2 S0", "M106 P3 S0",
            f"G1 Z{rest:.2f} F1200 ; bed down a little",
            f"G1 X{m.park_x:.0f} Y245 F12000", f"G1 Y{m.park_y:.0f} F3000", "M400"]
    if last:
        out.append(f";===== JOB COMPLETE: {s.quantity} units. Stock end G-code: filament back to the AMS, bed down =====")
    else:
        out.append(f";===== end of unit {unit}, starting unit {unit + 1} (filament stays loaded) =====")
    return out


def scale_numbers(text, q, nd=2):
    return re.sub(r"[\d.]+", lambda m: f"{float(m.group(0)) * q:.{nd}f}", text)


def write_output(job, s, p, out_path, progress=None):
    L, Q = job.lines, s.quantity
    m73_re = re.compile(r"M73 P(\d+) R(\d+)")
    layer_re = re.compile(r"M73 L(\d+)")
    count_re = re.compile(r"; layer num/total_layer_count: (\d+)/\d+")
    total_layers = job.layers * Q
    overhead = p.cycle_min - job.unit_min

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        w = f.write
        for l in L[:job.ex_start]:
            if l.startswith("; total layer number:"):
                l = f"; total layer number: {total_layers}"
            elif l.startswith("; model printing time:") and "total estimated time:" in l:
                l = re.sub(r"total estimated time: .*$", f"total estimated time: {cc.fmt_min(p.total_min)} 0s", l)
            elif re.match(r"; total filament (length|volume|weight) \[[^\]]+\] : ", l):
                k, v = l.split(" : ", 1)
                l = f"{k} : {scale_numbers(v, Q)}"
            w(l + "\n")
        w(L[job.ex_start] + "\n")
        w(f"; {APP_NAME} v{VERSION} ({P1S.name}): {Q} units, "
          f"{'cool %g min + to %dC' % (s.cool_floor, s.cool_temp) if s.cool_mode == 'temp' else 'cool %g min' % s.cool_minutes}, "
          f"push Z{p.push_z:g} lanes {p.push_lanes}, sweep Z{p.sweep_z}, "
          f"purge {'every unit' if s.purge_every else 'first unit only'} (bed sides)\n")
        for u in range(Q):
            w(f";===== SF UNIT {u + 1}/{Q} =====\n")
            lane = p.purge[u]
            for i in range(job.ex_start + 1, job.end_i):
                if job.purge_a <= i <= job.purge_b:
                    if i == job.purge_a:
                        g = lane_gcode(job, u + 1, *lane) if lane else no_purge_gcode(job, u + 1, p.no_purge_xy)
                        w("\n".join(g) + "\n")
                    continue
                l = L[i]
                if l.startswith("M73 "):
                    m = m73_re.match(l)
                    if m:
                        rem = int(m.group(2)) + overhead + (Q - 1 - u) * p.cycle_min
                        pct = max(0, min(99, round(100 * (1 - rem / p.total_min)))) if p.total_min else 0
                        l = f"M73 P{pct} R{int(round(rem))}"
                    else:
                        l = layer_re.sub(lambda m: f"M73 L{int(m.group(1)) + u * job.layers}", l)
                elif l.startswith("; layer num/total_layer_count:"):
                    l = count_re.sub(lambda m: f"; layer num/total_layer_count: {int(m.group(1)) + u * job.layers}"
                                               f"/{total_layers}", l)
                w(l + "\n")
            w("\n".join(eject_gcode(job, s, p, u + 1, u == Q - 1)) + "\n")
            if progress:
                progress(u + 1, Q)
        for l in L[job.end_i:job.ex_end + 1]:
            w(l + "\n")
        for l in L[job.ex_end + 1:]:
            if re.match(r"; (total )?filament (used|cost)", l):
                k, v = l.split("=", 1)
                l = f"{k}= {scale_numbers(v.strip(), Q)}"
            w(l + "\n")


def validate(job, s, p, out_path):
    """Re-read the written G-code and check it. Returns a list of (ok, label, detail)."""
    Q, m = s.quantity, P1S
    parts = job.part_rects
    unit, mode, rel = 0, None, False
    x = y = z = 0.0
    purge, print_ext = {}, {}
    m73, layers, oob = [], [], []
    g28 = cools = waits = eject_ext = end_tags = 0
    end_after_last = False
    cool_cmd = f"M190 S{s.cool_temp}"
    with open(out_path, encoding="utf-8") as f:
        for n, l in enumerate(f, 1):
            l = l.rstrip("\n")
            if l.startswith(";===== SF UNIT "):
                unit, mode = int(l.split()[3].split("/")[0]), "start"
                continue
            if l.startswith("; --- purge lane") or (l.startswith("; --- unit ") and "no purge" in l):
                mode = "purge"
            elif l in (";End purge lane.", ";End no purge."):
                mode = "start"
            elif mode == "start" and l.startswith("; CHANGE_LAYER"):
                mode = "print"
            elif l.startswith(";===== SF AUTO-EJECT"):
                mode = "eject"
            elif l.startswith(";===== JOB COMPLETE"):
                mode = "end"
            if l == job.end_tag:
                end_tags += 1
                end_after_last = mode == "end"
            if re.match(r"G28\b", l):
                g28 += 1
            elif mode == "eject" and l.startswith(cool_cmd):
                cools += 1
            elif mode == "eject" and l.startswith("M400 S"):
                waits += int(l.split()[1][1:])
            elif l.startswith("M73 "):
                mm = re.match(r"M73 P(\d+) R(\d+)", l)
                if mm:
                    m73.append((int(mm.group(1)), int(mm.group(2))))
                mm = re.match(r"M73 L(\d+)", l)
                if mm:
                    layers.append(int(mm.group(1)))
            elif l.startswith("G91"):
                rel = True
            elif l.startswith("G90"):
                rel = False
            if not is_move(l):
                continue
            nx, ny, nz, ext = x, y, z, False
            for k, v in MOVE_RE.findall(l.split(";", 1)[0]):
                v = float(v)
                if k == "X":
                    nx = x + v if rel else v
                elif k == "Y":
                    ny = y + v if rel else v
                elif k == "Z":
                    nz = z + v if rel else v
                elif k == "E" and v > 0:
                    ext = True
            if ext:
                if mode == "purge":
                    purge.setdefault(unit, []).append(seg_rect(x, y, nx, ny))
                elif mode == "print":
                    print_ext[unit] = print_ext.get(unit, 0) + 1
                elif mode == "eject":
                    eject_ext += 1
            if mode in ("purge", "eject") and not (0 <= nx <= m.bed and 0 <= ny <= m.park_y
                                                   and (0.2 <= nz <= m.z_max)):
                oob.append(n)
            x, y, z = nx, ny, nz

    checks = []

    def check(ok, label, detail):
        checks.append((bool(ok), label, detail))

    bad = [u for u in range(1, Q + 1) if print_ext.get(u, 0) != job.ext_count]
    check(not bad, "Print moves identical to the source",
          f"{job.ext_count:,} moves × {Q} units" if not bad else f"units {bad} differ")
    want_wait = Q * 60 * (s.cool_floor if s.cool_mode == "temp" else s.cool_minutes)
    cool_ok = abs(waits - want_wait) <= Q and (s.cool_mode != "temp" or cools == Q)
    check(g28 == Q * job.g28_per_unit and cool_ok, "Every unit re-runs the stock start and cools before the push",
          f"{g28 // max(1, job.g28_per_unit)} start sequences, {Q} cooldowns of {waits / Q / 60:.0f} min"
          + (f" + M190 S{s.cool_temp}" if s.cool_mode == "temp" else ""))
    want_tags = Q * job.end_tag_in_body + 1
    check(end_tags == want_tags and end_after_last, "Filament stays loaded until the stock end G-code, once, at the end",
          "end G-code after unit %d" % Q if end_tags == want_tags and end_after_last else f"{end_tags} end blocks")
    check(eject_ext == 0, "No extrusion during ejection", f"{eject_ext} extrusion moves found" if eject_ext else "clean")
    check(not oob, "Purge and ejection moves inside the machine",
          f"lines {oob[:5]}" if oob else f"X 0-{m.bed:g} · Y 0-{m.park_y:g} · Z 0.2-{m.z_max:g}")
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
    lay_ok = bool(layers) and (all(a <= b for a, b in zip(layers, layers[1:]))
                               and job.layers * (Q - 1) < max(layers) <= job.layers * Q)
    check(m73_ok and lay_ok, "Progress and layer counter run through the whole job",
          f"{job.layers * Q:,} layers" if lay_ok else "layer counter breaks")
    return checks


def patch_slice_info(xml, s, p):
    q = s.quantity
    xml = re.sub(r'(<metadata key="prediction" value=")\d+(")', lambda m: f"{m.group(1)}{int(p.total_min * 60)}{m.group(2)}", xml)
    xml = re.sub(r'(<metadata key="weight" value=")([\d.]+)(")',
                 lambda m: f"{m.group(1)}{float(m.group(2)) * q:.2f}{m.group(3)}", xml)
    return re.sub(r'(used_[mg]=")([\d.]+)(")', lambda m: f"{m.group(1)}{float(m.group(2)) * q:.2f}{m.group(3)}", xml)


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def write_3mf(job, s, p, gcode_path, out_path):
    """Copy the source .3mf with the batch G-code, its MD5 and the job totals swapped in."""
    md5 = file_md5(gcode_path)
    tmp = out_path + ".part"
    with zipfile.ZipFile(job.path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        names = set(zin.namelist())
        for it in zin.infolist():
            n = it.filename
            if n == job.plate:
                zout.write(gcode_path, n)
            elif n == job.plate + ".md5":
                zout.writestr(n, md5)
            elif n == SLICE_INFO:
                zout.writestr(n, patch_slice_info(zin.read(n).decode("utf-8"), s, p))
            else:
                zout.writestr(it, zin.read(n))
        if job.plate + ".md5" not in names:
            zout.writestr(job.plate + ".md5", md5)
    os.replace(tmp, out_path)


def check_3mf(job, out_path):
    with zipfile.ZipFile(out_path) as z:
        h = hashlib.md5()
        with z.open(job.plate) as g:
            for chunk in iter(lambda: g.read(1 << 20), b""):
                h.update(chunk)
        ok = z.read(job.plate + ".md5").decode().strip().upper() == h.hexdigest().upper()
        bad = z.testzip()
    return (ok and bad is None, "Printer package (.gcode.3mf) intact",
            f"{os.path.basename(job.plate)} MD5 {h.hexdigest().upper()[:8]}… matches" if ok and bad is None
            else "checksum mismatch: do not print")


def output_name(path, s):
    base = os.path.basename(path)
    low = base.lower()
    if low.endswith(".gcode.3mf"):
        stem, ext = base[:-10], ".gcode.3mf"
    elif low.endswith(".3mf"):
        stem, ext = base[:-4], ".gcode.3mf"
    else:
        stem, ext = os.path.splitext(base)[0], ".gcode"
    return f"{stem}_x{s.quantity}_AutoEject{ext}"


def build(job, s, out_path, progress=None):
    p = make_plan(job, s)
    if p.errors:
        return p, None, None
    if not job.is_3mf:
        write_output(job, s, p, out_path, progress)
        checks = validate(job, s, p, out_path)
        zip_path = None
        if s.make_zip and all(ok for ok, _, _ in checks):
            zip_path = os.path.splitext(out_path)[0] + ".zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                z.write(out_path, os.path.basename(out_path))
        return p, checks, zip_path
    gcode = out_path + ".gcode.part"
    try:
        write_output(job, s, p, gcode, progress)
        checks = validate(job, s, p, gcode)
        write_3mf(job, s, p, gcode, out_path)
    finally:
        if os.path.exists(gcode):
            os.remove(gcode)
    checks.append(check_3mf(job, out_path))
    return p, checks, None


plan_dict = cc.plan_dict
Job.engine = sys.modules[__name__]


if __name__ == "__main__":
    src = sys.argv[1]
    s = cc.Settings(quantity=int(sys.argv[2]) if len(sys.argv) > 2 else 10)
    job = Job(src)
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(src), output_name(src, s))
    p, checks, zp = build(job, s, out)
    d = plan_dict(job, s, p)
    print({k: d[k] for k in ("push_z", "push_rule", "sweep_z", "push_lanes", "sweep_lanes", "purge", "errors", "warnings")})
    for ok, label, detail in checks or []:
        print("OK  " if ok else "FAIL", label, "-", detail)
    sys.exit(1 if p.errors or not checks or not all(c[0] for c in checks) else 0)
