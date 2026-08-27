"""
Local Video Transcriber
Paste any video link (YouTube, Vimeo, X, etc.) -> downloads audio -> transcribes with timestamps.
Runs entirely on your PC. Open http://localhost:5005 after starting.
"""

import logging
import os
import re
import threading
import time
import uuid

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from flask import Flask, jsonify, request, Response

# Silence the per-request log lines (status polling floods the console)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(APP_DIR, "audio_cache")
OUT_DIR = os.path.join(APP_DIR, "transcripts")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__)

# job_id -> job state dict
JOBS = {}
JOBS_LOCK = threading.Lock()

# Loaded whisper models, keyed by model size
MODELS = {}
MODELS_LOCK = threading.Lock()


def fmt_ts(seconds, srt=False):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if srt:
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\n\r]+', "", title)
    # keep filenames ASCII-safe (emoji/symbols break HTTP headers and some tools)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:120] or "transcript")


def get_model(size):
    with MODELS_LOCK:
        if size not in MODELS:
            from faster_whisper import WhisperModel
            MODELS[size] = WhisperModel(size, device="cpu", compute_type="int8")
        return MODELS[size]


def set_job(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)


def run_job(job_id, url, model_size):
    try:
        # ---------- 1. Download audio ----------
        set_job(job_id, status="downloading", progress=0, message="Downloading audio...")
        import yt_dlp

        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    pct = d.get("downloaded_bytes", 0) / total * 100
                    set_job(job_id, progress=round(pct, 1),
                            message=f"Downloading audio... {pct:.0f}%")

        outtmpl = os.path.join(AUDIO_DIR, f"{job_id}.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "progress_hooks": [hook],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title = info.get("title", "Untitled")
        duration = info.get("duration") or 0
        audio_path = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(job_id):
                audio_path = os.path.join(AUDIO_DIR, f)
                break
        if not audio_path:
            raise RuntimeError("Audio download failed - no file produced.")

        # ---------- 2. Load model ----------
        set_job(job_id, status="loading_model", progress=0, title=title,
                message=f"Loading Whisper model '{model_size}' (first time downloads it)...")
        model = get_model(model_size)

        # ---------- 3. Transcribe ----------
        set_job(job_id, status="transcribing", progress=0, message="Transcribing...")
        segments_iter, tr_info = model.transcribe(audio_path, vad_filter=True)
        total_dur = duration or tr_info.duration or 1

        segments = []
        for seg in segments_iter:
            segments.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
            pct = min(seg.end / total_dur * 100, 99.9)
            set_job(job_id, progress=round(pct, 1),
                    message=f"Transcribing... {pct:.0f}% ({fmt_ts(seg.end)} / {fmt_ts(total_dur)})")

        # ---------- 4. Save outputs ----------
        base = safe_filename(title)
        txt_path = os.path.join(OUT_DIR, base + ".txt")
        srt_path = os.path.join(OUT_DIR, base + ".srt")

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"{title}\n{url}\n\n")
            for s in segments:
                f.write(f"[{fmt_ts(s['start'])}] {s['text']}\n")

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, s in enumerate(segments, 1):
                f.write(f"{i}\n{fmt_ts(s['start'], srt=True)} --> {fmt_ts(s['end'], srt=True)}\n{s['text']}\n\n")

        # Clean up audio file
        try:
            os.remove(audio_path)
        except OSError:
            pass

        set_job(job_id, status="done", progress=100, message="Done", title=title,
                segments=segments, txt_file=os.path.basename(txt_path),
                srt_file=os.path.basename(srt_path),
                language=tr_info.language)
    except Exception as e:
        set_job(job_id, status="error", message=str(e))


@app.route("/api/transcribe", methods=["POST"])
def start_transcribe():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    model_size = data.get("model", "base")
    if model_size not in ("tiny", "base", "small", "medium", "large-v3"):
        model_size = "base"
    if not url or not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "Please enter a valid video URL."}), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Queued...",
                        "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, url, model_size), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify(job)


@app.route("/download/<path:fname>")
def download(fname):
    path = os.path.join(OUT_DIR, os.path.basename(fname))
    if not os.path.isfile(path):
        return "Not found", 404
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # ASCII-safe fallback name + RFC 5987 encoded full name (headers must be latin-1)
    from urllib.parse import quote
    base = os.path.basename(fname)
    ascii_name = base.encode("ascii", "ignore").decode("ascii").strip() or "transcript.txt"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(base)}"
    return Response(content, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": disposition})


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Video Transcriber</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background:#111418; color:#e8eaed;
         max-width: 860px; margin: 0 auto; padding: 32px 20px; }
  h1 { font-size: 1.6em; margin-bottom: 4px; }
  .sub { color:#9aa0a6; margin-bottom: 24px; }
  .card { background:#1b1f24; border:1px solid #2c3138; border-radius:12px; padding:20px; margin-bottom:20px; }
  input[type=url] { width:100%; box-sizing:border-box; padding:12px 14px; font-size:15px; border-radius:8px;
          border:1px solid #3c4148; background:#0e1114; color:#e8eaed; }
  select { padding:10px 12px; border-radius:8px; border:1px solid #3c4148; background:#0e1114; color:#e8eaed; font-size:14px; }
  button { padding:11px 24px; font-size:15px; font-weight:600; border:none; border-radius:8px;
           background:#4f8ef7; color:#fff; cursor:pointer; }
  button:disabled { background:#3c4148; cursor:default; }
  .row { display:flex; gap:12px; margin-top:12px; align-items:center; flex-wrap:wrap; }
  .hint { color:#9aa0a6; font-size:13px; }
  #bar-wrap { background:#0e1114; border-radius:6px; height:10px; overflow:hidden; margin-top:10px; display:none; }
  #bar { background:#4f8ef7; height:100%; width:0%; transition:width .4s; }
  #msg { margin-top:10px; color:#9aa0a6; font-size:14px; min-height:20px; }
  #result { display:none; }
  .seg { padding:6px 0; border-bottom:1px solid #23272d; line-height:1.5; }
  .ts { color:#4f8ef7; font-family:Consolas,monospace; font-size:13px; margin-right:10px; cursor:pointer; }
  .dl { margin-right:14px; color:#8ab4f8; }
  .err { color:#f28b82; }
  #title { font-weight:600; margin-bottom:10px; }
</style>
</head>
<body>
<h1>&#127908; Video Transcriber</h1>
<div class="sub">Paste any video link &mdash; runs 100% locally on your PC</div>

<div class="card">
  <input type="url" id="url" placeholder="https://www.youtube.com/watch?v=..." autofocus>
  <div class="row">
    <label class="hint">Model:
      <select id="model">
        <option value="tiny">tiny &mdash; fastest, least accurate</option>
        <option value="base" selected>base &mdash; fast, good</option>
        <option value="small">small &mdash; slower, better</option>
        <option value="medium">medium &mdash; slow, best on CPU</option>
      </select>
    </label>
    <button id="go" onclick="start()">Transcribe</button>
  </div>
  <div id="bar-wrap"><div id="bar"></div></div>
  <div id="msg"></div>
</div>

<div class="card" id="result">
  <div id="title"></div>
  <div style="margin-bottom:14px">
    <a class="dl" id="dl-txt" href="#">&#11015; Download .txt</a>
    <a class="dl" id="dl-srt" href="#">&#11015; Download .srt (subtitles)</a>
    <span class="hint" id="lang"></span>
  </div>
  <div id="segments"></div>
</div>

<script>
let poller = null;

function fmtTs(sec) {
  const h = String(Math.floor(sec/3600)).padStart(2,'0');
  const m = String(Math.floor(sec%3600/60)).padStart(2,'0');
  const s = String(Math.floor(sec%60)).padStart(2,'0');
  return h + ':' + m + ':' + s;
}

async function start() {
  const url = document.getElementById('url').value.trim();
  const model = document.getElementById('model').value;
  if (!url) return;
  document.getElementById('go').disabled = true;
  document.getElementById('result').style.display = 'none';
  document.getElementById('bar-wrap').style.display = 'block';
  document.getElementById('bar').style.width = '0%';
  setMsg('Starting...');
  const r = await fetch('/api/transcribe', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({url, model})});
  const j = await r.json();
  if (j.error) { fail(j.error); return; }
  poller = setInterval(() => poll(j.job_id), 1000);
}

async function poll(id) {
  const r = await fetch('/api/status/' + id);
  const j = await r.json();
  if (j.error) { fail(j.error); return; }
  document.getElementById('bar').style.width = (j.progress || 0) + '%';
  setMsg(j.message || '');
  if (j.status === 'error') fail(j.message);
  if (j.status === 'done') { clearInterval(poller); showResult(j); }
}

function showResult(j) {
  document.getElementById('go').disabled = false;
  document.getElementById('bar').style.width = '100%';
  setMsg('Done \\u2014 saved to the transcripts folder.');
  document.getElementById('title').textContent = j.title;
  document.getElementById('lang').textContent = 'Detected language: ' + (j.language || '?');
  document.getElementById('dl-txt').href = '/download/' + encodeURIComponent(j.txt_file);
  document.getElementById('dl-srt').href = '/download/' + encodeURIComponent(j.srt_file);
  const box = document.getElementById('segments');
  box.innerHTML = '';
  for (const s of j.segments) {
    const div = document.createElement('div');
    div.className = 'seg';
    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = '[' + fmtTs(s.start) + ']';
    div.appendChild(ts);
    div.appendChild(document.createTextNode(s.text));
    box.appendChild(div);
  }
  document.getElementById('result').style.display = 'block';
}

function fail(m) {
  clearInterval(poller);
  document.getElementById('go').disabled = false;
  document.getElementById('msg').innerHTML = '<span class="err">Error: ' +
    m.replace(/</g,'&lt;') + '</span>';
}

function setMsg(m) { document.getElementById('msg').textContent = m; }

document.getElementById('url').addEventListener('keydown', e => {
  if (e.key === 'Enter') start();
});
</script>
</body>
</html>"""


@app.route("/")
def index():
    return PAGE


if __name__ == "__main__":
    print("\n  Video Transcriber running -> open http://localhost:5005 in your browser\n")
    app.run(host="127.0.0.1", port=5005, debug=False)
