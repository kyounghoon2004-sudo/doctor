"""Interactive web dashboard for the YouTube Shorts pipeline.

Run:
    python dashboard.py            # then open http://127.0.0.1:5000
    python dashboard.py --port 8000 --no-open

Features:
    * Start / Stop the pipeline (channel, hashtag, or explicit URLs).
    * Live progress bar, log stream, and a results grid with screenshots.
    * Direct link to the spreadsheet: download or open it in Excel.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import webbrowser
from datetime import datetime

# Force UTF-8 console so non-Latin summaries (Korean, etc.) and werkzeug logs
# don't crash on legacy Windows code pages like cp949.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import (
    Flask,
    abort,
    jsonify,
    request,
    send_file,
    send_from_directory,
)

import config
from runner import runner

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return PAGE_HTML


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/api/start")
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    source_type = (data.get("source_type") or "").strip()
    source_value = (data.get("source_value") or "").strip()
    try:
        max_count = int(data.get("max_count") or config.MAX_SHORTS_PER_TARGET)
    except (TypeError, ValueError):
        max_count = config.MAX_SHORTS_PER_TARGET
    headless = bool(data.get("headless", True))
    summary_language = (data.get("summary_language") or config.DEFAULT_SUMMARY_LANGUAGE).strip()
    content_type = "videos" if (data.get("content_type") == "videos") else "shorts"

    if source_type not in {"channel", "search", "urls"}:
        return jsonify({"ok": False, "error": "Invalid source type."}), 400
    if not source_value:
        return jsonify({"ok": False, "error": "Please provide a value."}), 400
    if runner.is_running():
        return jsonify({"ok": False, "error": "A run is already in progress."}), 409

    started = runner.start(
        source_type, source_value, max_count, headless, summary_language, content_type
    )
    return jsonify({"ok": started})


@app.post("/api/stop")
def api_stop():
    stopped = runner.stop()
    return jsonify({"ok": stopped})


@app.post("/api/reset")
def api_reset():
    """Start a fresh session: archive the current spreadsheet and clear the
    in-memory logs/results so the next run writes a brand-new workbook."""
    if runner.is_running():
        return jsonify({"ok": False, "error": "Stop the current run before resetting."}), 409

    archived = None
    try:
        if config.EXCEL_FILE.exists():
            archive_dir = config.BASE_DIR / "archive"
            archive_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = archive_dir / f"{config.EXCEL_FILE.stem}_{stamp}{config.EXCEL_FILE.suffix}"
            shutil.move(str(config.EXCEL_FILE), str(dest))
            archived = dest.name
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not archive spreadsheet: {exc}"}), 500

    runner.reset()
    return jsonify({"ok": True, "archived": archived})


@app.get("/api/status")
def api_status():
    snap = runner.snapshot()
    snap["spreadsheet_path"] = str(config.EXCEL_FILE)
    snap["spreadsheet_exists"] = config.EXCEL_FILE.exists()
    return jsonify(snap)


@app.get("/screenshots/<path:filename>")
def screenshot(filename: str):
    if not config.SCREENSHOTS_DIR.exists():
        abort(404)
    return send_from_directory(config.SCREENSHOTS_DIR, filename)


@app.get("/spreadsheet")
def spreadsheet():
    """Download the Excel workbook."""
    if not config.EXCEL_FILE.exists():
        abort(404, "Spreadsheet has not been created yet.")
    return send_file(config.EXCEL_FILE, as_attachment=True)


@app.post("/api/open-spreadsheet")
def open_spreadsheet():
    """Open the workbook in the system's default app (local convenience)."""
    if not config.EXCEL_FILE.exists():
        return jsonify({"ok": False, "error": "Spreadsheet not created yet."}), 404
    try:
        if hasattr(os, "startfile"):  # Windows
            os.startfile(str(config.EXCEL_FILE))  # noqa: S606
        else:  # macOS / Linux best effort
            import subprocess
            opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
            subprocess.Popen([opener, str(config.EXCEL_FILE)])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Front-end (single page, no build step)
# ---------------------------------------------------------------------------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Shorts Processor — Dashboard</title>
<style>
  :root{
    --bg:#0f1117; --panel:#171a23; --panel2:#1d2130; --border:#2a2f40;
    --text:#e7e9ee; --muted:#9aa2b1; --accent:#ef4444; --accent2:#3b82f6;
    --ok:#22c55e; --warn:#f59e0b; --err:#ef4444;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:16px 22px;
         border-bottom:1px solid var(--border);background:var(--panel)}
  header h1{font-size:17px;margin:0;font-weight:650}
  header .logo{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;
       background:linear-gradient(135deg,#ef4444,#b91c1c);font-size:16px}
  .wrap{display:grid;grid-template-columns:340px 1fr;gap:18px;padding:18px;
        max-width:1280px;margin:0 auto}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
  .card h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
           margin:0 0 12px}
  label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
  input[type=text],input[type=number],select{
     width:100%;background:var(--panel2);border:1px solid var(--border);color:var(--text);
     border-radius:8px;padding:9px 10px;font-size:14px;outline:none}
  input:focus,select:focus{border-color:var(--accent2)}
  .seg{display:flex;gap:6px;background:var(--panel2);border:1px solid var(--border);
       border-radius:10px;padding:4px}
  .seg button{flex:1;background:transparent;border:0;color:var(--muted);padding:8px;
       border-radius:7px;cursor:pointer;font-size:13px}
  .seg button.active{background:var(--accent2);color:#fff}
  .row{display:flex;gap:10px}
  .row>*{flex:1}
  .check{display:flex;align-items:center;gap:8px;margin-top:12px;color:var(--muted)}
  .btns{display:flex;gap:10px;margin-top:16px}
  button.run,button.stop{flex:1;border:0;border-radius:10px;padding:12px;font-weight:650;
       cursor:pointer;font-size:14px}
  button.run{background:var(--ok);color:#04210f}
  button.stop{background:var(--accent);color:#fff}
  button.reset{width:100%;margin-top:10px;background:var(--panel2);color:var(--text);
       border:1px solid var(--border);border-radius:10px;padding:10px;cursor:pointer;font-size:13px}
  button.reset:hover:not(:disabled){border-color:var(--accent2)}
  button:disabled{opacity:.45;cursor:not-allowed}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
       font-size:12px;font-weight:600;background:var(--panel2);border:1px solid var(--border)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  .dot.running{background:var(--accent2);animation:pulse 1s infinite}
  .dot.done{background:var(--ok)} .dot.error{background:var(--err)}
  .dot.stopping,.dot.stopped{background:var(--warn)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  .progress{height:10px;background:var(--panel2);border-radius:999px;overflow:hidden;margin:12px 0 6px}
  .progress>div{height:100%;width:0;background:linear-gradient(90deg,#3b82f6,#22c55e);
       transition:width .4s}
  .meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}
  .ssbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px}
  .ssbar a,.ssbar button{font-size:13px}
  .linkbtn{background:var(--panel2);border:1px solid var(--border);color:var(--text);
       padding:8px 12px;border-radius:8px;text-decoration:none;cursor:pointer;display:inline-block}
  .linkbtn.primary{background:var(--accent2);border-color:var(--accent2);color:#fff}
  .path{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--muted);
       word-break:break-all;margin-top:8px}
  .log{background:#0b0d13;border:1px solid var(--border);border-radius:10px;padding:10px;
       height:200px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .log .l-info{color:#cbd5e1}.log .l-warn{color:var(--warn)}.log .l-error{color:var(--err)}
  .log .t{color:#64748b;margin-right:8px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-top:8px}
  .shot{background:var(--panel2);border:1px solid var(--border);border-radius:12px;overflow:hidden;
        display:flex;flex-direction:column}
  .shot img{width:100%;height:200px;object-fit:cover;background:#000}
  .shot .body{padding:10px}
  .kind{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:999px;
        margin-bottom:6px;border:1px solid var(--border)}
  .kind.k-vision{background:#3b2a59;color:#c4b5fd}
  .kind.k-none{background:#3a2e1a;color:#fcd34d}
  .kind.k-speech{background:#10241a;color:#86efac}
  .shot .sum{font-size:13px;margin:0 0 8px}
  .shot a{color:var(--accent2);font-size:12px;text-decoration:none}
  .empty{color:var(--muted);text-align:center;padding:40px 10px}
  .toast{position:fixed;bottom:18px;right:18px;background:var(--panel2);border:1px solid var(--border);
        padding:12px 16px;border-radius:10px;opacity:0;transform:translateY(10px);
        transition:.25s;max-width:320px}
  .toast.show{opacity:1;transform:none}
  @media(max-width:880px){.wrap{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">▶</div>
  <h1>YouTube Shorts — Processing Dashboard</h1>
  <span id="badge" class="badge"><span id="dot" class="dot"></span><span id="stateText">idle</span></span>
</header>

<div class="wrap">
  <!-- Controls -->
  <div class="card" style="align-self:start">
    <h2>Run a job</h2>
    <label>Source</label>
    <div class="seg" id="seg">
      <button data-t="search" class="active">Search</button>
      <button data-t="channel">Channel</button>
      <button data-t="urls">URLs</button>
    </div>

    <label>Type</label>
    <div class="seg" id="typeSeg">
      <button data-c="shorts" class="active">Shorts</button>
      <button data-c="videos">Videos</button>
    </div>

    <label id="valLabel">Describe what to watch</label>
    <input id="value" type="text" placeholder="e.g. recent AI developments"/>

    <div class="row">
      <div>
        <label>Max results</label>
        <input id="max" type="number" min="1" value="5"/>
      </div>
    </div>

    <div class="check">
      <input id="headless" type="checkbox" checked/>
      <label for="headless" style="margin:0">Headless browser</label>
    </div>

    <div class="btns">
      <button id="runBtn" class="run">▶ Run</button>
      <button id="stopBtn" class="stop" disabled>■ Stop</button>
    </div>
    <button id="resetBtn" class="reset" title="Archive the current spreadsheet and clear the log + results">↻ New session (reset)</button>

    <h2 style="margin-top:22px">Spreadsheet</h2>
    <div class="ssbar">
      <a id="dlBtn" class="linkbtn primary" href="/spreadsheet">⬇ Download .xlsx</a>
      <button id="openBtn" class="linkbtn">📂 Open in Excel</button>
    </div>
    <div class="path" id="ssPath">—</div>
  </div>

  <!-- Results / progress -->
  <div style="display:flex;flex-direction:column;gap:18px">
    <div class="card">
      <h2>Progress</h2>
      <div class="progress"><div id="bar"></div></div>
      <div class="meta">
        <span id="counts">0 / 0 processed</span>
        <span id="src"></span>
      </div>
      <h2 style="margin-top:16px">Live log</h2>
      <div class="log" id="log"><div class="empty">No activity yet.</div></div>
    </div>

    <div class="card">
      <h2>Results</h2>
      <div id="results"><div class="empty">Results will appear here as videos finish.</div></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let sourceType = "search";
let contentType = "shorts";
let polling = null;

const $ = (id)=>document.getElementById(id);
const labels = {
  search:["Describe what to watch","e.g. recent AI developments"],
  channel:["Channel name or @handle","e.g. MrBeast"],
  urls:["Comma-separated video URLs","https://www.youtube.com/shorts/ID  or  https://www.youtube.com/watch?v=ID"]
};

$("seg").addEventListener("click",(e)=>{
  const b=e.target.closest("button"); if(!b) return;
  [...$("seg").children].forEach(x=>x.classList.remove("active"));
  b.classList.add("active"); sourceType=b.dataset.t;
  $("valLabel").textContent=labels[sourceType][0];
  $("value").placeholder=labels[sourceType][1];
});

$("typeSeg").addEventListener("click",(e)=>{
  const b=e.target.closest("button"); if(!b) return;
  [...$("typeSeg").children].forEach(x=>x.classList.remove("active"));
  b.classList.add("active"); contentType=b.dataset.c;
});

function toast(msg){const t=$("toast");t.textContent=msg;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),2600);}

$("runBtn").onclick = async ()=>{
  const payload={source_type:sourceType,source_value:$("value").value,
    max_count:parseInt($("max").value||"5",10),headless:$("headless").checked,
    content_type:contentType};
  if(!payload.source_value.trim()){toast("Enter a value first.");return;}
  const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
     body:JSON.stringify(payload)});
  const j=await r.json();
  if(!j.ok){toast(j.error||"Could not start.");return;}
  toast("Run started.");startPolling();refresh();
};

$("stopBtn").onclick = async ()=>{
  const r=await fetch("/api/stop",{method:"POST"});const j=await r.json();
  toast(j.ok?"Stopping after current video…":"Nothing is running.");refresh();
};

$("resetBtn").onclick = async ()=>{
  if(!confirm("Start a new session?\n\nThe current spreadsheet will be archived to the /archive folder, and the log + results will be cleared. A fresh spreadsheet is created on your next run.")) return;
  const r=await fetch("/api/reset",{method:"POST"});const j=await r.json();
  if(!j.ok){toast(j.error||"Could not reset.");return;}
  toast(j.archived?("Archived "+j.archived+" — new session ready."):"New session ready.");
  refresh();
};

$("openBtn").onclick = async ()=>{
  const r=await fetch("/api/open-spreadsheet",{method:"POST"});const j=await r.json();
  toast(j.ok?"Opening spreadsheet…":(j.error||"Could not open."));
};

function startPolling(){ if(polling) return; polling=setInterval(refresh,1500); }
function stopPolling(){ clearInterval(polling); polling=null; }

function render(s){
  // state badge
  $("stateText").textContent=s.state;
  $("dot").className="dot "+s.state;
  // buttons
  $("runBtn").disabled=s.running;
  $("stopBtn").disabled=!s.running;
  $("resetBtn").disabled=s.running;
  // progress
  const pct=s.total?Math.round(100*s.processed/s.total):0;
  $("bar").style.width=pct+"%";
  $("counts").textContent=`${s.processed} / ${s.total} processed`;
  $("src").textContent=s.source_label||"";
  // spreadsheet
  $("ssPath").textContent=s.spreadsheet_path||"—";
  $("dlBtn").style.opacity=s.spreadsheet_exists?1:.45;
  $("openBtn").style.opacity=s.spreadsheet_exists?1:.45;
  // log
  const log=$("log");
  if(s.logs && s.logs.length){
    log.innerHTML=s.logs.map(l=>`<div class="l-${l.level}"><span class="t">${l.time}</span>${escapeHtml(l.msg)}</div>`).join("");
    log.scrollTop=log.scrollHeight;
  } else {
    log.innerHTML='<div class="empty">No activity yet.</div>';
  }
  // results
  const res=$("results");
  if(s.results && s.results.length){
    res.innerHTML='<div class="grid">'+s.results.slice().reverse().map(r=>`
      <div class="shot">
        ${r.screenshot?`<img loading="lazy" src="/screenshots/${encodeURIComponent(r.screenshot)}" alt="">`:`<div style="height:200px;display:grid;place-items:center;color:#475569">no image</div>`}
        <div class="body">
          ${kindBadge(r.kind)}
          <p class="sum">${escapeHtml(r.summary||"")}</p>
          <a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">▶ open Short (${escapeHtml(r.video_id||"")})</a>
        </div>
      </div>`).join("")+'</div>';
  } else {
    res.innerHTML='<div class="empty">Results will appear here as videos finish.</div>';
  }
  // stop polling when finished
  if(!s.running && ["done","stopped","error","idle"].includes(s.state)) stopPolling();
}

function kindBadge(kind){
  if(kind==="vision") return '<span class="kind k-vision">👁 visual context</span>';
  if(kind==="none") return '<span class="kind k-none">🔇 no speech</span>';
  if(kind==="speech") return '<span class="kind k-speech">🎤 transcribed</span>';
  return "";
}
function escapeHtml(t){return (t||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function escapeAttr(t){return (t||"").replace(/"/g,"&quot;");}

async function refresh(){
  try{const r=await fetch("/api/status");render(await r.json());}catch(e){}
}

// initial load: render once; if a run is already active, begin polling
refresh().then(()=>fetch("/api/status").then(r=>r.json()).then(s=>{if(s.running)startPolling();}));
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Shorts pipeline web dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open a browser.")
    args = parser.parse_args()

    config.ensure_directories()
    url = f"http://{args.host}:{args.port}"
    print(f"\n  Shorts dashboard running at {url}\n  Press Ctrl+C to stop.\n")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # use_reloader=False so the background runner thread isn't duplicated.
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
