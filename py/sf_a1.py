"""SolidForms Auto-Eject engine, Bambu Lab A1 (also the A1 Combo with the AMS lite): turn a 1-plate Bambu Studio /
OrcaSlicer G-code (.gcode, or a .gcode.3mf "plate sliced file") into one N-unit job that cools, pushes the part off the
front of the bed, and starts again.

Built on the P1S engine (sf_p1s): same parsing, cooldown, 3MF packaging and self-check. What is A1-specific:
- the A1 is a bed-slinger: Y moves the BED. The push is the same move in printer coordinates (nozzle from behind the
  part to the front edge), but physically the bed slides back under a nozzle that stays put, and the part drops off
  the bed's front edge in the MIDDLE of the printer, under the gantry. A forward tilt or a ramp under the bed carries
  it out (that is what the commercial A1 auto-eject kits add);
- the A1 purges and wipes off the bed (chute at X-48), so there is no load line to move. But every print touches the
  bed at the front edge (X108 Y-0.5, "G2814") and draws a short flow-calibration line there (2024+ start G-code).
  Units 2+ would touch and print onto the previous unit's line, so each unit's calibration line is moved to its own
  clean spot on a bed side, outside the band the parts are pushed through. The touch spot itself stays where Bambu
  put it, and stays clean;
- every unit re-runs the stock A1 start (heat, home, flush into the chute, nozzle wipe, bed levelling, vibration
  check). Filament stays loaded; the stock end G-code (filament back to the AMS lite) runs once, after the last unit;
- cooling parks the toolhead off the bed on the left (the stock end G-code's safe spot, X-13).

The A1 mini is a different machine (180 mm bed) and is not supported.

CLI: python sf_a1.py <in.gcode|in.gcode.3mf> [quantity] [out]
"""
import os
import re
import sys

import sf_engine as cc
import sf_p1s as bb
from sf_engine import JobError, Machine, MOVE_RE, is_move, rect_hit, grow

MACHINE = A1 = Machine(
    "a1", "Bambu Lab A1", "bambu lab a1", "the stock Bambu Lab A1 printer profile (Bambu Studio or OrcaSlicer)",
    z_max=256.0, lane_x_min=10.0, lane_x_max=246.0, stage_y=256.0, approach_y=15.0, final_y=1.0,
    park_x=-13.0, park_y=256.0, back_limit_y=226.0, back_warn_y=212.0, rod_height=25.0,
    x_min=-48.5, y_min=-1.0, purge_z_min=0.0,
    marks=((128.0, 261.0, "Z HOME · NOZZLE WIPE"),
           (128.0, -0.5, "NOZZLE TOUCH", "The A1 touches the bed's front edge here before every print. "
                                         "Its calibration line is moved to the bed sides, so this spot stays clean.")))
APPROACH_F, FINAL_F = cc.APPROACH_F, cc.FINAL_F
EJECT_ACCEL = 2000
PARK_X = -13.0                      # stock end G-code: "G1 X-13.0 ; move to safe pos"
END_X, END_Y = -48.0, 180.0         # stock end G-code's last spot; the next unit's start begins from here
Y_SCALE = 3.0                       # the stock lines sit 1.5 mm apart because the first hangs off the edge; on the plate they need more
LINE_HALF = 2.0                     # the first stretch of each calibration line is a wide blob (up to 4 mm)
SPOT_GAP = 2.5                      # clear space between two units' spots
SPAN_MAX = 70.0                     # longer = the old 2023 start G-code (105 mm lines): re-slice
ROW_Y0, ROW_Y_MAX = 8.0, 236.0      # calibration rows stay off the front edge and in front of the push staging area


def is_a1(model):
    return model.strip().lower() == "bambu lab a1"


def sets_z_before_extruding(L, start, end):
    """True if an absolute Z move comes before the first extruding move from `start` on."""
    rel = False
    for i in range(start, min(end, len(L))):
        code = L[i].split(";", 1)[0].strip()
        if code.startswith("G91"):
            rel = True
        elif code.startswith("G90"):
            rel = False
        elif is_move(code):
            v = dict(MOVE_RE.findall(code))
            if "Z" in v and not rel:
                return True
            if float(v.get("E", 0)) > 0:
                return False
    return False


class Job(bb.Job):
    """A parsed single-plate Bambu Lab A1 G-code (Bambu Studio or OrcaSlicer)."""
    machine = A1

    def model_ok(self, model):
        return is_a1(model)

    def start_lines(self, L):
        # ";===== machine: A1" also heads the filament-change G-code of multi-colour prints: count only print starts
        out = []
        for i in range(self.ex_start, self.ex_end):
            if L[i].startswith(";===== machine:") and any("start to heat" in L[j] for j in range(i, min(i + 12, self.ex_end))):
                out.append(i)
        return out

    def find_purge(self, L, first):
        """The front-edge calibration block -> moved to a bed side, one clean spot per unit."""
        self.purge_a = self.purge_b = self.load_temp = None
        self.cal = None
        self.cal_problem = None
        lo, hi = self.ex_start, self.layer0
        cali = first(lambda l: l.startswith(";===== extrude cali test"), lo, hi)
        if cali is None:
            self.cal_problem = "The A1 calibration line was not found in the start G-code."
            self.can_purge = False
            return
        load = first(lambda l: l.startswith(";===== nozzle load line"), lo, cali)
        a = load if load is not None else cali
        off = first(lambda l: l.startswith(";========turn off light"), cali, hi)
        if off is None:
            self.cal_problem = "The end of the A1 calibration block was not found."
            self.can_purge = False
            return
        b = next((i for i in range(off - 1, cali, -1) if L[i].startswith("G1 Z0.2")), off - 1)
        # where the nozzle is when the block starts: the G2814 front-edge touch spot (2024+ start G-code)
        x = y = None
        rel = False
        touch = first(lambda l: l.startswith("G2814"), lo, a)
        for i in range(lo, touch if touch is not None else lo):
            l = L[i]
            if l.startswith("G91"):
                rel = True
            elif l.startswith("G90"):
                rel = False
            elif is_move(l) and not rel:
                v = dict(MOVE_RE.findall(l.split(";", 1)[0]))
                x = float(v["X"]) if "X" in v else x
                y = float(v["Y"]) if "Y" in v else y
        # the block's own moves: their extent decides the size of each unit's spot
        xs, ys, rel = [], [], False
        cx, cy = x, y
        for i in range(a, b + 1):
            code = L[i].split(";", 1)[0].strip()
            if code.startswith("G91"):
                rel = True
            elif code.startswith("G90"):
                rel = False
            elif is_move(code):
                v = dict(MOVE_RE.findall(code))
                if rel:
                    cx = cx + float(v.get("X", 0)) if cx is not None else None
                    cy = cy + float(v.get("Y", 0)) if cy is not None else None
                else:
                    cx = float(v["X"]) if "X" in v else cx
                    cy = float(v["Y"]) if "Y" in v else cy
                if cx is not None and cy is not None:
                    xs.append(cx)
                    ys.append(cy)
            elif code.startswith("M109 S") and self.load_temp is None:
                self.load_temp = code
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
        if not xs or self.load_temp is None:
            self.cal_problem = "The A1 calibration line's moves could not be read."
            self.can_purge = False
            return
        span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        if span_x > SPAN_MAX:
            self.cal_problem = (f"This file uses the old 2023 A1 start G-code ({span_x:.0f} mm calibration lines). "
                                "Re-slice it with a current Bambu Studio or OrcaSlicer.")
            self.can_purge = False
            return
        self.purge_a, self.purge_b = a, b
        self.cal = {"x_ref": min(xs), "y_ref": min(ys), "w": span_x, "h": span_y * Y_SCALE,
                    "start": (x if x is not None else min(xs), y if y is not None else min(ys))}
        # The stock block ends at Z0.2 and the print's first travel starts from there. From a bed side that travel could
        # drag over an earlier unit's line, so the block ends 1 mm up instead, but only when the print itself sets the
        # nozzle height before its first extrusion (Bambu Studio and Orca do: "G1 Z.4 / G1 Z.2").
        self.lift_ok = L[b].startswith("G1 Z0.2") and sets_z_before_extruding(L, b + 1, hi + 2000)
        self.can_purge = True

    def on_bed(self, r):
        # colour changes flush into the chute at X-48 and wipe there: off the bed, not part of any part
        return r[1] > 0 and r[0] < A1.bed and r[3] > 0 and r[2] < A1.bed

    def spot_rect(self, x0, y0):
        c = self.cal
        return (x0 - LINE_HALF, x0 + c["w"] + LINE_HALF, y0 - LINE_HALF, y0 + c["h"] + LINE_HALF)

    def plan_purge(self, s, p, fp):
        """One calibration-line spot per unit, on the bed sides outside the parts' X band. Returns the sweep span."""
        E, m = p.errors, A1
        if self.cal_problem:
            p.errors[:] = [e for e in E if not e.startswith("The start G-code's purge line was not recognised")]
            E.append(self.cal_problem)
        if not s.purge_every:
            E.append("On the A1 every unit draws its own short calibration line (the printer also measures the flow "
                     "with it), so 'First unit only' is not available. Choose 'Every unit'.")
        a, b = m.lane_x_min, m.lane_x_max
        if self.cal is None:
            p.purge = [None] * s.quantity
            return a, b
        w, h = self.cal["w"], self.cal["h"]
        col = w + 2 * LINE_HALF + SPOT_GAP
        pitch = max(s.purge_pitch, h + 2 * LINE_HALF + SPOT_GAP)
        left, right = [], []
        x = m.edge + LINE_HALF
        while x + w + LINE_HALF <= p.left_limit:
            left.append(round(x, 2))
            x += col
        x = m.bed - m.edge - LINE_HALF - w
        while x - LINE_HALF >= p.right_limit:
            right.append(round(x, 2))
            x -= col
        rows = []
        y = ROW_Y0
        while y + h + LINE_HALF <= ROW_Y_MAX:
            rows.append(round(y, 2))
            y += pitch
        parts = [grow(r, 2) for r in self.part_rects]
        slots = []
        for y in rows:
            for i in range(max(len(left), len(right))):
                for side, xs in (("L", left), ("R", right)):
                    if i < len(xs) and not any(rect_hit(self.spot_rect(xs[i], y), q) for q in parts):
                        slots.append((xs[i], y, side))
        if len(slots) < s.quantity:
            room = "no room beside the part" if not slots else f"only {len(slots)} fit beside the part"
            E.append(f"Each unit needs its own {w + 2 * LINE_HALF:.0f} mm calibration-line spot on a bed side, and there is "
                     f"{room} (need {s.quantity}). Make the batch smaller, or centre / narrow the part so a bed side "
                     f"has {w + 2 * LINE_HALF + m.purge_gap:.0f} mm free.")
        p.purge = [slots[u] if u < len(slots) else None for u in range(s.quantity)]
        used = [sl for sl in p.purge if sl]
        lx = max([self.spot_rect(x, y)[1] for x, y, side in used if side == "L"], default=None)
        rx = min([self.spot_rect(x, y)[0] for x, y, side in used if side == "R"], default=None)
        a = max((lx + m.sweep_keepout) if lx is not None else 0.0, m.lane_x_min)
        b = min((rx - m.sweep_keepout) if rx is not None else m.bed, m.lane_x_max)
        return a, b

    def check(self, s, p):
        super().check(s, p)
        p.warnings.append("A1: the bed slides back under the nozzle, so the part drops off the bed's front edge in the "
                          "middle of the printer. Tilt the printer forward (about 20°) or fit a ramp under the bed so "
                          "parts slide out the front, keep a bin there, and watch the first two ejections.")


def lane_gcode(job, unit, x0, y0, side):
    """The stock calibration block, moved to (x0, y0): same commands, same extrusion, same speeds."""
    c = job.cal
    dx = x0 - c["x_ref"]

    def ny(v):
        return y0 + (v - c["y_ref"]) * Y_SCALE

    out = [f"; --- purge lane, unit {unit}: A1 calibration line moved to the {'left' if side == 'L' else 'right'} side, "
           f"X{x0:.1f}-{x0 + c['w']:.1f} Y{y0:.1f}-{y0 + c['h']:.1f} (stock: the front edge, X108 Y-0.5) ---",
           "G90",
           "G1 Z1 F1200 ; lift off the front-edge touch point",
           f"G1 X{x0 + c['start'][0] - c['x_ref']:.3f} Y{ny(c['start'][1]):.3f} F30000",
           "G1 Z0.3 F1200"]
    rel = False
    for i, l in enumerate(job.lines[job.purge_a:job.purge_b + 1], job.purge_a):
        if i == job.purge_b and job.lift_ok:
            out.append("G1 Z1 F1200 ; (stock: Z0.2) stay clear of the other units' lines on the way to the part")
            continue
        code, sep, comment = l.partition(";")
        t = code.strip()
        if t.startswith("G91"):
            rel = True
        elif t.startswith("G90"):
            rel = False
        elif is_move(t) and not rel:
            code = re.sub(r"\bX(-?\d*\.?\d+)", lambda mm: f"X{float(mm.group(1)) + dx:.3f}", code)
            code = re.sub(r"\bY(-?\d*\.?\d+)", lambda mm: f"Y{ny(float(mm.group(1))):.3f}", code)
        out.append((code + sep + comment).strip())   # no indent: the self-check reads these lines too
    out += ["M400", ";End purge lane."]
    return out


def no_purge_gcode(job, unit, xy):      # never used: the A1 plan refuses "first unit only"
    raise JobError("The A1 needs a calibration line for every unit.")


def eject_gcode(job, s, p, unit, last):
    m, H = A1, job.height
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
        f"G1 Z{lift:.2f} F900 ; toolhead up, clear of the part top",
        f"G1 X{PARK_X:.0f} F12000 ; off the bed to the left (the stock end G-code's safe spot)",
        "M400",
        "M104 S0 ; hotend off",
        "M140 S0 ; bed heater off",
        "M106 S255 ; part fan max",
    ]
    if s.cool_mode == "temp":
        out += bb.wait_minutes(s.cool_floor, f"guaranteed cooling: {s.cool_floor:g} min")
        out += [f"M190 S{s.cool_temp} ; then wait for the bed to read {s.cool_temp}C",
                "M140 S0 ; clear the M190 target: heater stays off"]
    else:
        out += bb.wait_minutes(s.cool_minutes, f"cool {s.cool_minutes:g} min, then push regardless")
    out += ["M400", f"M204 S{EJECT_ACCEL} ; gentle accelerations for pushing",
            f"; --- push at Z{p.push_z:g} ({p.push_rule}): the bed slides back, the nozzle holds the part, "
            "it drops off the front edge ---",
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
    out += ["M106 S0",
            f"G1 Z{rest:.2f} F1200 ; toolhead up",
            f"G1 X{PARK_X:.0f} F12000",
            f"G1 X{END_X:.0f} Y{END_Y:.0f} F3600 ; where the stock end G-code leaves the toolhead", "M400"]
    if last:
        out.append(f";===== JOB COMPLETE: {s.quantity} units. Stock end G-code: filament back to the AMS lite =====")
    else:
        out.append(f";===== end of unit {unit}, starting unit {unit + 1} (filament stays loaded) =====")
    return out


def plan_dict(job, s, p):
    d = cc.plan_dict(job, s, p)
    d["purge_rects"] = [list(map(lambda v: round(v, 2), job.spot_rect(sl[0], sl[1]))) if sl else None for sl in p.purge]
    return d


make_plan, write_output, validate, output_name = cc.make_plan, bb.write_output, bb.validate, bb.output_name
build = bb.build
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
