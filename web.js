// Online Auto-Eject: stands in for the desktop app (pywebview) so ui/index.html runs unchanged in a browser.
// The engines run in worker.js; files are read from the user's disk and the batch is downloaded back. Nothing is uploaded.
(() => {
  const BUILD = document.documentElement.dataset.build || "";
  const worker = new Worker("worker.js?v=" + BUILD);
  let seq = 0, isReady = false, inputPath = null, lastResult = null;
  const pending = new Map();
  const call = (cmd, extra = {}, transfer = []) => new Promise((resolve, reject) => {
    const id = ++seq; pending.set(id, { resolve, reject });
    worker.postMessage({ id, cmd, ...extra }, transfer);
  });
  let markReady; const ready = new Promise((r) => (markReady = r));

  // a small banner while the engine (Python, ~10 MB) loads the first time; later visits come from the browser cache
  const banner = document.createElement("div");
  banner.id = "webBanner";
  banner.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:999;background:#121318;border:1px solid rgba(255,255,255,.14);color:#ECEAE6;padding:10px 16px;border-radius:6px;font:13px 'Segoe UI',system-ui,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.5)";
  banner.textContent = "Loading the Auto-Eject engine… (first visit takes a few seconds)";
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(banner));

  worker.onmessage = ({ data: m }) => {
    if (m.type === "ready") { isReady = true; banner.remove(); markReady(); window.dispatchEvent(new Event("pywebviewready")); return; }
    if (m.type === "failed") { banner.textContent = "The engine could not load: " + m.error + ". Check the internet connection and reload."; banner.style.borderColor = "#ff5a4e"; return; }
    if (m.type === "progress") { window.onProgress && window.onProgress(m.d, m.n); return; }
    const p = pending.get(m.id); if (!p) return; pending.delete(m.id);
    m.error ? p.reject(new Error(m.error)) : p.resolve(m.result);
  };

  const download = (bytes, name, type) => {
    const url = URL.createObjectURL(new Blob([bytes], { type }));
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  };

  async function takeFile(file) {
    if (!file) return null;
    if (!/\.(gcode|3mf)$/i.test(file.name)) return file.name;     // the page shows its own "not a print file" message
    await ready;
    const buf = await file.arrayBuffer();
    inputPath = await call("put", { name: file.name, buf }, [buf]);   // moved to the engine, not copied
    return inputPath;
  }

  window.pywebview = {
    api: {
      info: async () => { await ready; return call("info"); },
      browse: () => new Promise((resolve) => {
        const inp = Object.assign(document.createElement("input"), { type: "file", accept: ".gcode,.3mf" });
        inp.onchange = async () => resolve(await takeFile(inp.files[0]).catch((e) => ({ error: e.message })));
        inp.addEventListener("cancel", () => resolve(null));
        inp.click();
      }),
      analyze: async (path, settings) => { await ready; return call("analyze", { path, settings }).catch((e) => ({ ok: false, error: e.message })); },
      generate: async (path, settings) => {
        await ready;
        const r = await call("generate", { path, settings }).catch((e) => ({ ok: false, error: e.message }));
        if (r.ok) {
          lastResult = r;
          download(r.data, r.name, "application/octet-stream");
          r.out = r.name;
          setTimeout(() => {   // the desktop's "Show in folder" becomes downloads here
            const btn = document.getElementById("reveal");
            if (btn) btn.textContent = "Download again";
            const card = document.getElementById("resName");
            if (card && r.zipData && !document.getElementById("zipLink")) {
              const z = Object.assign(document.createElement("a"), { id: "zipLink", href: "#", textContent: `Download the .zip copy (${r.zip_mb} MB)` });
              z.style.cssText = "display:block;margin-top:6px;color:#FF7A1A;font-size:12px";
              z.onclick = (e) => { e.preventDefault(); download(lastResult.zipData, lastResult.zip_name, "application/zip"); };
              card.after(z);
            }
          }, 50);
        }
        return r;
      },
      reveal: () => { if (lastResult) download(lastResult.data, lastResult.name, "application/octet-stream"); },
    },
  };

  // drag a file onto the page (the page itself only stops the browser from opening it)
  document.addEventListener("drop", async (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (!f) return;
    const p = await takeFile(f).catch(() => null);
    if (p && window.onDropPath) window.onDropPath(p);
  });
})();
