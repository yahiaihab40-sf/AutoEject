"""Browser bridge for the online Auto-Eject: the same engines as the desktop app, run by Pyodide in a Web Worker.

Mirrors app.Api (info / analyze / generate) without pywebview. Files live in Pyodide's in-memory disk: the page writes the
dropped file to /work/in, the batch is written to /work/out and handed back to the page as a download. Nothing is uploaded.
"""
import json
import os
import time
import traceback

import engines
import sf_engine as eng

IN_DIR, OUT_DIR = "/work/in", "/work/out"
os.makedirs(IN_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)
_job = None


def _get_job(path):
    global _job
    if _job is None or _job.path != path or _job.mtime != os.path.getmtime(path):
        _job = engines.open_job(path)
    return _job


def put_file(name, data):
    """Store a dropped file (bytes) as the only input; earlier inputs and outputs are cleared to free memory."""
    global _job
    _job = None
    for d in (IN_DIR, OUT_DIR):
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
    path = os.path.join(IN_DIR, os.path.basename(name) or "plate.gcode")
    with open(path, "wb") as f:
        f.write(bytes(data))
    return path


def info():
    return json.dumps({"app": eng.APP_NAME, "version": eng.VERSION, "defaults": eng.asdict(eng.Settings())})


def analyze(path, settings_json):
    try:
        if not path or not os.path.isfile(path):
            return json.dumps({"ok": False, "error": "File not found."})
        job = _get_job(path)
        s = eng.settings_from(json.loads(settings_json))
        plan = job.engine.plan_dict(job, s, job.engine.make_plan(job, s))
        plan["size_mb"] = round(os.path.getsize(path) / 1e6, 1)
        plan["folder"] = "stays on this computer"
        return json.dumps({"ok": True, "plan": plan})
    except eng.JobError as e:
        return json.dumps({"ok": False, "error": str(e)})
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"ok": False, "error": f"Could not read this file ({e})."})


def generate(path, settings_json, progress=None):
    try:
        job = _get_job(path)
        s = eng.settings_from(json.loads(settings_json))
        for f in os.listdir(OUT_DIR):
            os.remove(os.path.join(OUT_DIR, f))
        out = os.path.join(OUT_DIR, job.engine.output_name(path, s))
        t0 = time.time()
        p, checks, zip_path = job.engine.build(job, s, out, (lambda d, n: progress(d, n)) if progress else None)
        if p.errors:
            return json.dumps({"ok": False, "error": " ".join(p.errors)})
        return json.dumps({
            "ok": True,
            "out": out,
            "name": os.path.basename(out),
            "zip": zip_path,
            "zip_name": os.path.basename(zip_path) if zip_path else None,
            "size_mb": round(os.path.getsize(out) / 1e6, 1),
            "zip_mb": round(os.path.getsize(zip_path) / 1e6, 1) if zip_path else None,
            "seconds": round(time.time() - t0, 1),
            "passed": all(ok for ok, _, _ in checks),
            "checks": [{"ok": ok, "label": label, "detail": detail} for ok, label, detail in checks],
        })
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"ok": False, "error": f"Generation failed: {e}"})
