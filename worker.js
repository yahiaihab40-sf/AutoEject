// Runs the Auto-Eject engines (Python) in the browser with Pyodide, off the page's main thread so the screen stays smooth.
// Messages in: { id, cmd: "info" | "put" | "analyze" | "generate", ... }. Out: { id, result } / { id, error } / { type: "progress" }.
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.28.3/full/";
importScripts(PYODIDE + "pyodide.js");
const FILES = ["sf_engine.py", "sf_p1s.py", "sf_a1.py", "engines.py", "web_api.py"];
const BUILD = new URL(location.href).searchParams.get("v") || "";

const ready = (async () => {
  const py = await loadPyodide({ indexURL: PYODIDE });
  py.FS.mkdirTree("/app");
  for (const f of FILES) {
    const r = await fetch("py/" + f + "?v=" + BUILD, { cache: "no-cache" });
    if (!r.ok) throw new Error(`Could not load ${f} (${r.status})`);
    py.FS.writeFile("/app/" + f, await r.text());
  }
  py.runPython("import sys; sys.path.insert(0, '/app'); import web_api");
  return py;
})();
ready.then(() => postMessage({ type: "ready" }), (e) => postMessage({ type: "failed", error: String(e && e.message || e) }));

const api = (py) => py.pyimport("web_api");
onmessage = async ({ data: m }) => {
  try {
    const py = await ready, w = api(py);
    let result;
    if (m.cmd === "info") result = JSON.parse(w.info());
    else if (m.cmd === "put") result = w.put_file(m.name, py.toPy(new Uint8Array(m.buf)));
    else if (m.cmd === "analyze") result = JSON.parse(w.analyze(m.path, JSON.stringify(m.settings)));
    else if (m.cmd === "generate") {
      const progress = (d, n) => postMessage({ type: "progress", d, n });
      result = JSON.parse(w.generate(m.path, JSON.stringify(m.settings), progress));
      if (result.ok) {
        // hand the finished files to the page as bytes (moved, not copied)
        result.data = py.FS.readFile(result.out).buffer;
        if (result.zip) result.zipData = py.FS.readFile(result.zip).buffer;
        postMessage({ id: m.id, result }, [result.data, ...(result.zipData ? [result.zipData] : [])]);
        return;
      }
    }
    postMessage({ id: m.id, result });
  } catch (e) {
    postMessage({ id: m.id, error: String(e && e.message || e) });
  }
};
