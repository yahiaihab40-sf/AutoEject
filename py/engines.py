"""Pick the engine for a sliced file: Elegoo Centauri Carbon (sf_engine), Bambu Lab P1S (sf_p1s) or Bambu Lab A1 (sf_a1)."""
import os
import re
import zipfile

import sf_engine
import sf_p1s
import sf_a1
from sf_engine import JobError

ENGINES = (sf_engine, sf_p1s)
SUPPORTED = ("the Elegoo Centauri Carbon (.gcode), the Bambu Lab P1S and the Bambu Lab A1 / A1 Combo "
             "(.gcode or .gcode.3mf)")
SNIFF = 600_000     # printer_model sits in the config block: top (Bambu) or bottom (Orca) of the file
MODEL_RE = re.compile(rb"^; printer_model = (.+?)\r?$", re.M)
BAMBU_IDS = {"N2S": "Bambu Lab A1", "N1": "Bambu Lab A1 mini"}   # slice_info printer_model_id, if the header has no name


def sniff_model(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(SNIFF)
        if size > 2 * SNIFF:
            f.seek(-SNIFF, 2)
        tail = f.read()
    m = MODEL_RE.search(head + b"\n" + tail)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def sniff_3mf_model(path):
    """The printer a .gcode.3mf was sliced for ("" if it can't be told here: the P1S reader then explains)."""
    try:
        with zipfile.ZipFile(path) as z:
            plates = [n for n in z.namelist() if sf_p1s.PLATE_RE.match(n)]
            if len(plates) != 1:
                return ""
            with z.open(plates[0]) as f:
                m = MODEL_RE.search(f.read(SNIFF))
            if m:
                return m.group(1).decode("utf-8", "replace").strip()
            if sf_p1s.SLICE_INFO in z.namelist():
                i = re.search(r'key="printer_model_id" value="([^"]+)"', z.read(sf_p1s.SLICE_INFO).decode("utf-8", "replace"))
                if i:
                    return BAMBU_IDS.get(i.group(1), "")
    except (zipfile.BadZipFile, OSError):
        pass
    return ""


def refuse_mini(model):
    if "a1 mini" in model.lower():
        raise JobError("Sliced for the Bambu Lab A1 mini. Auto-Eject supports the full-size A1 / A1 Combo "
                       "(256 mm bed), not the A1 mini.")


def open_job(path):
    if path.lower().endswith(".3mf"):
        model = sniff_3mf_model(path)
        refuse_mini(model)
        return sf_a1.Job(path) if sf_a1.is_a1(model) else sf_p1s.Job(path)
    model = sniff_model(path)
    refuse_mini(model)
    if sf_a1.is_a1(model):
        return sf_a1.Job(path)
    for eng in ENGINES:
        if eng.MACHINE.match in model.lower():
            return eng.Job(path)
    raise JobError(f"Sliced for '{model or 'an unknown printer'}'. This app supports {SUPPORTED}.")
