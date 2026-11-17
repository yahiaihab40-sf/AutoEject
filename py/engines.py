"""Pick the engine for a sliced file: Elegoo Centauri Carbon (sf_engine) or Bambu Lab P1S (sf_p1s)."""
import os
import re

import sf_engine
import sf_p1s
from sf_engine import JobError

ENGINES = (sf_engine, sf_p1s)
SUPPORTED = "the Elegoo Centauri Carbon (.gcode) and the Bambu Lab P1S (.gcode or .gcode.3mf)"
SNIFF = 600_000     # printer_model sits in the config block: top (Bambu) or bottom (Orca) of the file


def sniff_model(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(SNIFF)
        if size > 2 * SNIFF:
            f.seek(-SNIFF, 2)
        tail = f.read()
    m = re.search(rb"^; printer_model = (.+?)\r?$", head + b"\n" + tail, re.M)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def open_job(path):
    if path.lower().endswith(".3mf"):
        return sf_p1s.Job(path)
    model = sniff_model(path)
    for eng in ENGINES:
        if eng.MACHINE.match in model.lower():
            return eng.Job(path)
    raise JobError(f"Sliced for '{model or 'an unknown printer'}'. This app supports {SUPPORTED}.")
