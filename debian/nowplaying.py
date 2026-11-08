#!/usr/bin/env python3
"""Now-playing page for the TV: track data and position from AirPlay, synced lyrics from LRCLIB,
music levels from the HDMI/S-PDIF output."""
import base64
import collections
import datetime
import hashlib
import json
import math
import os
import plistlib
import re
import select
import stat
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIPE = os.environ.get("NP_PIPE", "/tmp/shairport-sync-metadata")
PORT = int(os.environ.get("NP_PORT", "8099"))
STATUS = os.environ.get("NP_STATUS", "/proc/asound/card0/pcm4p/sub0/status")
DEMO = os.environ.get("NP_DEMO") == "1"
DEMO_LYRICS = os.environ.get("NP_DEMO_LYRICS")
LYRICS_DIR = os.environ.get("NP_LYRICS_DIR", "/root/lyrics-cache")
FONT_DIR = os.environ.get("NP_FONT_DIR", "/usr/share/fonts/opentype/inter")
FONTS = {"800": "Inter-ExtraBold.otf", "600": "Inter-SemiBold.otf"}
MAX_BUFFER = 4 * 1024 * 1024

SAMPLE_RATE = 48000
HOP = 1600  # frames between updates: 30 per second
FFT_SIZE = 2048
BYTES_PER_HOP = HOP * 4
BASS_BAND = (30, 160)
RANGE_DB = 24.0
SILENCE_RMS = 1.2e-4
LOOPBACK_CONTROL = "name=Audio spdifin source"
CAPTURE = ["arecord", "-q", "-D", "hw:0,4", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "2"]

LRCLIB = "https://lrclib.net/api"
HEADERS = {"User-Agent": "airplay-nowplaying/1.0"}
MR = "kMRMediaRemoteNowPlayingInfo"
STATE_DIR = os.environ.get("NP_STATE_DIR", "/root/np-state")
DELAY_FILE = os.environ.get("NP_DELAY_FILE", "/root/np-lyrics-delay")
DEFAULT_DELAY = 2.0
VOCAL_BAND = (300, 3000)

HEAD = re.compile(rb"<type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>")
DATA = re.compile(rb'<data encoding="base64">\s*([A-Za-z0-9+/=\s]*?)\s*</data>')
LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
TITLE_NOISE = re.compile(r"\s*[\(\[][^\)\]]*(feat\.?|ft\.|with |remaster|version|live|edit|mix)[^\)\]]*[\)\]]", re.I)
TITLE_DASH = re.compile(r"\s+-\s+.*(remaster|version|live|edit|mix).*$", re.I)

state = {"title": "", "artist": "", "album": "", "cover": 0}
cover = {"bytes": b"", "digest": ""}
playback = {"elapsed": 0.0, "rate": 0.0, "at": 0.0, "duration": 0.0}
lyrics = {"key": "", "lines": []}
lock = threading.Lock()
calibration = collections.deque(maxlen=30 * 200)  # (reported position, voice-band level), one track at a time
lyrics_wanted = threading.Event()

level_value = "0"
levels_cond = threading.Condition()

TEXT_FIELDS = {"minm": "title", "asar": "artist", "asal": "album"}


def decode(hexstr):
    return bytes.fromhex(hexstr.decode()).decode("latin1")


def apply_now_playing(raw):
    """Takes the player's own report of its position, which covers seeks and pauses exactly."""
    try:
        message = plistlib.loads(raw)
    except Exception:
        return
    if not isinstance(message, dict) or message.get("type") != "updateMRNowPlayingInfo":
        return
    info = message.get("params", {}).get("params", {})
    elapsed = info.get(MR + "ElapsedTime")
    if elapsed is None:
        return
    rate = float(info.get(MR + "PlaybackRate", 1.0))
    lag = 0.0
    stamp = info.get(MR + "Timestamp")
    if isinstance(stamp, datetime.datetime):
        lag = time.time() - stamp.replace(tzinfo=datetime.timezone.utc).timestamp()
        if abs(lag) > 4:  # the two clocks disagree; trust the arrival time instead
            lag = 0.0
    with lock:
        playback["elapsed"] = float(elapsed) + lag * rate
        playback["rate"] = rate
        playback["at"] = time.monotonic()
        if info.get(MR + "Duration"):
            playback["duration"] = float(info[MR + "Duration"])


def handle(item):
    head = HEAD.search(item)
    if not head:
        return
    kind, code = decode(head.group(1)), decode(head.group(2))
    match = DATA.search(item)

    if kind == "ssnc" and code in ("pend", "paus"):
        with lock:
            playback["rate"] = 0.0
        return
    if not match:
        return
    try:
        payload = base64.b64decode(match.group(1), validate=False)
    except ValueError:
        return

    if kind == "ssnc" and code == "copl":
        apply_now_playing(payload)
        return

    with lock:
        if kind == "core" and code in TEXT_FIELDS:
            field = TEXT_FIELDS[code]
            text = payload.decode("utf-8", "replace").strip()
            if state[field] != text:
                state[field] = text
                lyrics_wanted.set()
                if field == "title":
                    # A new track must not keep showing the previous cover or position.
                    cover["bytes"] = b""
                    cover["digest"] = ""
                    state["cover"] += 1
                    playback.update(elapsed=0.0, rate=1.0, at=time.monotonic())
                    calibration.clear()
        elif kind == "ssnc" and code == "PICT" and payload:
            digest = hashlib.sha1(payload).hexdigest()
            if digest != cover["digest"]:
                cover["bytes"] = payload
                cover["digest"] = digest
                state["cover"] += 1


def metadata_reader():
    buf = b""
    while True:
        if not os.path.exists(PIPE):
            time.sleep(1)
            continue
        is_fifo = stat.S_ISFIFO(os.stat(PIPE).st_mode)
        with open(PIPE, "rb", buffering=0) as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BUFFER:
                    buf = buf[-MAX_BUFFER:]
                while True:
                    end = buf.find(b"</item>")
                    if end < 0:
                        break
                    handle(buf[: end + 7])
                    buf = buf[end + 7 :]
        if not is_fifo:
            return
        time.sleep(1)


def parse_lrc(text):
    stamped = []
    for raw in text.splitlines():
        tags = LRC_TAG.findall(raw)
        if not tags:
            continue
        words = LRC_TAG.sub("", raw).strip()
        for minutes, seconds in tags:
            stamped.append((int(minutes) * 60 + float(seconds), words))
    stamped.sort(key=lambda x: x[0])
    lines = []
    for i, (t, words) in enumerate(stamped):
        if not words:
            continue
        end = stamped[i + 1][0] if i + 1 < len(stamped) else t + 5.0
        lines.append({"t": round(t, 2), "end": round(min(end, t + 12.0), 2), "text": words})
    return lines


def fetch_json(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def lookup_lyrics(title, artist, album, duration):
    clean = TITLE_DASH.sub("", TITLE_NOISE.sub("", title)).strip() or title
    query = {"track_name": clean, "artist_name": artist, "album_name": album}
    if duration:
        query["duration"] = int(round(duration))
    try:
        found = fetch_json(LRCLIB + "/get?" + urllib.parse.urlencode(query))
        if found.get("syncedLyrics"):
            return parse_lrc(found["syncedLyrics"])
    except Exception:
        pass
    try:
        results = fetch_json(LRCLIB + "/search?" + urllib.parse.urlencode({"track_name": clean, "artist_name": artist}))
    except Exception:
        return None  # network trouble: not the same as "this song has no lyrics"
    synced = [r for r in results if r.get("syncedLyrics")]
    if duration:
        synced = [r for r in synced if abs((r.get("duration") or 0) - duration) <= 8]
        synced.sort(key=lambda r: abs((r.get("duration") or 0) - duration))
    return parse_lrc(synced[0]["syncedLyrics"]) if synced else []


def lyrics_worker():
    os.makedirs(LYRICS_DIR, exist_ok=True)
    while True:
        lyrics_wanted.wait()
        time.sleep(0.8)  # let the title, artist, album and duration of one track all arrive
        lyrics_wanted.clear()
        with lock:
            title, artist, album = state["title"], state["artist"], state["album"]
            duration = playback["duration"]
        key = f"{title}|{artist}"
        if not title or key == lyrics["key"]:
            continue
        path = os.path.join(LYRICS_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")
        lines = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    lines = json.load(f)
            except (OSError, ValueError):
                lines = None
        if lines is None:
            lines = lookup_lyrics(title, artist, album, duration)
            if lines:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(lines, f, ensure_ascii=False)
        with lock:
            if state["title"] == title and state["artist"] == artist:
                lyrics["key"] = key
                lyrics["lines"] = lines or []


def save_state():
    """Written on every change, so restarting this service does not blank the screen mid-song."""
    with lock:
        snapshot = {"state": dict(state), "wall": time.time(), "rate": playback["rate"], "duration": playback["duration"],
                    "elapsed": playback["elapsed"] + (time.monotonic() - playback["at"]) * playback["rate"]}
        art = cover["bytes"]
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "state.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False)
    os.replace(os.path.join(STATE_DIR, "state.json.tmp"), os.path.join(STATE_DIR, "state.json"))
    with open(os.path.join(STATE_DIR, "cover.bin"), "wb") as f:
        f.write(art)


def load_state():
    try:
        with open(os.path.join(STATE_DIR, "state.json"), encoding="utf-8") as f:
            snapshot = json.load(f)
        with open(os.path.join(STATE_DIR, "cover.bin"), "rb") as f:
            art = f.read()
    except (OSError, ValueError):
        return
    if not output_running():
        return
    age = max(0.0, time.time() - snapshot["wall"])
    with lock:
        state.update(snapshot["state"])
        cover["bytes"] = art
        cover["digest"] = hashlib.sha1(art).hexdigest() if art else ""
        playback.update(elapsed=snapshot["elapsed"] + age * snapshot["rate"], rate=snapshot["rate"],
                        at=time.monotonic(), duration=snapshot["duration"])
    lyrics_wanted.set()


def state_saver():
    seen = None
    while True:
        time.sleep(2)
        with lock:
            now = (json.dumps(state, sort_keys=True), playback["at"], playback["rate"], cover["digest"])
        if now != seen and state["title"]:
            seen = now
            try:
                save_state()
            except OSError:
                pass


def publish(bass):
    global level_value
    with levels_cond:
        level_value = f"{bass:.3f}"
        levels_cond.notify_all()


def output_running():
    try:
        with open(STATUS) as f:
            return "RUNNING" in f.read(64)
    except OSError:
        return False


def set_loopback(value):
    subprocess.run(["amixer", "-q", "-c0", "cset", LOOPBACK_CONTROL, str(value)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


class Analyzer:
    """Follows the bass and adapts to the volume, so quiet playback still moves."""

    def __init__(self, np):
        self.np = np
        self.window = np.hanning(FFT_SIZE)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / SAMPLE_RATE)
        self.mask = (freqs >= BASS_BAND[0]) & (freqs < BASS_BAND[1])
        self.vocal_mask = (freqs >= VOCAL_BAND[0]) & (freqs < VOCAL_BAND[1])
        self.peak = -60.0
        self.vocal_db = None

    def feed(self, block):
        np = self.np
        self.vocal_db = None
        if float(np.sqrt(np.mean(block**2))) < SILENCE_RMS:
            return 0.0
        spectrum = np.abs(np.fft.rfft(block * self.window)) ** 2
        self.vocal_db = 10 * float(np.log10(spectrum[self.vocal_mask].mean() + 1e-14))
        db = 10 * float(np.log10(spectrum[self.mask].mean() + 1e-14))
        self.peak = max(db, self.peak - 0.04)
        return min(1.0, max(0.0, (db - (self.peak - RANGE_DB)) / RANGE_DB)) ** 1.7


def stop_capture(proc):
    if proc is not None:
        proc.kill()
        proc.wait()
    set_loopback(0)
    publish(0)


def capture_loop():
    import numpy as np

    analyzer = Analyzer(np)
    block = np.zeros(FFT_SIZE)
    proc = None
    pending = b""
    hops = 0
    while True:
        if proc is None:
            if not output_running():
                time.sleep(0.3)
                continue
            set_loopback(1)
            proc = subprocess.Popen(CAPTURE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            pending = b""

        # The capture stalls once the output stops, so never block on it for long.
        ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        chunk = os.read(proc.stdout.fileno(), BYTES_PER_HOP) if ready else None
        hops += 1
        if chunk == b"" or chunk is None or hops % 30 == 0:
            if chunk == b"" or not output_running():
                stop_capture(proc)
                proc = None
                continue
            if chunk is None:
                continue

        pending += chunk
        while len(pending) >= BYTES_PER_HOP:
            frames = np.frombuffer(pending[:BYTES_PER_HOP], dtype="<i2").reshape(-1, 2).mean(axis=1) / 32768.0
            pending = pending[BYTES_PER_HOP:]
            block = np.roll(block, -HOP)
            block[-HOP:] = frames
            publish(analyzer.feed(block))
            if analyzer.vocal_db is not None:
                with lock:
                    rate = playback["rate"]
                    position = playback["elapsed"] + (time.monotonic() - playback["at"]) * rate
                    if rate > 0:
                        calibration.append((position, analyzer.vocal_db))


def lyrics_delay():
    try:
        with open(DELAY_FILE) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return DEFAULT_DELAY


def measure_delay():
    """Finds how far the reported position runs ahead of the sound, from where sung lines really begin."""
    import numpy as np

    with lock:
        samples = list(calibration)
        times = [line["t"] for line in lyrics["lines"]]
    if len(samples) < 900 or len(times) < 6:
        return {"ok": False, "samples": len(samples), "lines": len(times)}
    data = np.array(samples)
    data = data[np.argsort(data[:, 0])]
    pos, db = data[:, 0], data[:, 1]
    smooth = np.convolve(db, np.ones(3) / 3, mode="same")
    earlier = np.concatenate([np.full(5, smooth[0]), smooth[:-5]])
    onset = np.clip(smooth - earlier, 0, None)
    onset = onset - onset.mean()
    lags = np.arange(-1.0, 6.001, 0.02)
    inside = np.array([t for t in times if pos[0] + 1.5 < t < pos[-1] - 6.5])
    if len(inside) < 6:
        return {"ok": False, "samples": len(samples), "lines": int(len(inside))}
    scores = []
    for lag in lags:
        index = np.clip(np.searchsorted(pos, inside + lag), 3, len(onset) - 4)
        scores.append(float(np.mean([onset[i - 3 : i + 4].max() for i in index])))
    scores = np.array(scores)
    best = int(np.argmax(scores))
    spread = float(scores.std()) or 1e-9
    order = np.argsort(scores)[::-1]
    others = [round(float(lags[i]), 2) for i in order if abs(lags[i] - lags[best]) > 0.3][:3]
    return {"ok": True, "delay": round(float(lags[best]), 2), "confidence": round(float((scores[best] - scores.mean()) / spread), 1),
            "next_best": others, "lines_used": int(len(inside)), "seconds": round(float(pos[-1] - pos[0]), 1)}


def demo_loop():
    """Simulated playback, used only to preview the page away from the box."""
    time.sleep(0.5)
    with lock:
        playback.update(elapsed=27.0, rate=1.0, at=time.monotonic(), duration=188.0)
        lyrics["key"] = "demo"
        if DEMO_LYRICS:
            with open(DEMO_LYRICS, encoding="utf-8") as f:
                lyrics["lines"] = parse_lrc(json.load(f)["syncedLyrics"])
    start = time.time()
    while True:
        beat = ((time.time() - start) * 2.0) % 1.0
        publish(max(0.0, 1.0 - beat * 2.4) ** 1.2)
        time.sleep(1 / 30)


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Сейчас играет</title>
<style>
  @font-face { font-family: Inter; font-weight: 800; src: url(/font/800); }
  @font-face { font-family: Inter; font-weight: 600; src: url(/font/600); }
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; background: #07070a; overflow: hidden; }
  body { color: #fff; font-family: Inter, Roboto, -apple-system, "Helvetica Neue", sans-serif; font-weight: 600; }
  #gl { position: fixed; inset: 0; width: 100%; height: 100%; opacity: 0; transition: opacity 1.4s ease; }

  #left { position: fixed; top: 0; bottom: 0; left: 6.5vw; width: 30vw; display: flex; flex-direction: column;
          justify-content: center; transition: left .9s cubic-bezier(.22, 1, .36, 1), width .9s cubic-bezier(.22, 1, .36, 1); }
  #frame { width: 100%; aspect-ratio: 1; border-radius: 1.1vw; overflow: hidden; position: relative;
           background: rgba(255,255,255,.08); box-shadow: 0 1.8vw 4.5vw rgba(0,0,0,.4); will-change: transform; }
  #art { width: 100%; height: 100%; object-fit: cover; display: block; opacity: 0; transition: opacity .6s ease; }
  #meta { margin-top: 2vw; }
  #title { font-size: 2.1vw; font-weight: 800; letter-spacing: -.02em; line-height: 1.15;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #artist { margin-top: .35vw; font-size: 1.5vw; color: rgba(255,255,255,.66);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #bar { margin-top: 1.3vw; height: .28vw; border-radius: .2vw; background: rgba(255,255,255,.2); overflow: hidden; }
  #fill { height: 100%; width: 100%; background: rgba(255,255,255,.9); transform-origin: left; transform: scaleX(0); }

  body.plain #left { left: 50%; width: 36vw; margin-left: -18vw; }
  body.plain #title { font-size: 2.6vw; white-space: normal; text-align: center; }
  body.plain #artist { font-size: 1.8vw; text-align: center; }

  #view { position: fixed; top: 0; bottom: 0; left: 42vw; right: 3vw; overflow: hidden; transition: opacity .6s ease;
          -webkit-mask-image: linear-gradient(180deg, transparent 0, #000 16%, #000 80%, transparent 100%); }
  body.plain #view { opacity: 0; }
  #lyr { position: absolute; left: 0; right: 0; top: 0; will-change: transform;
         transition: transform .85s cubic-bezier(.22, 1, .36, 1); }
  .ln { font-size: 4.3vw; font-weight: 800; line-height: 1.17; letter-spacing: -.028em; padding: 1.25vw 0;
        color: rgba(255,255,255,.22); transform-origin: left center; transition: color .45s ease, transform .6s cubic-bezier(.22, 1, .36, 1); }
  .ln.past { color: rgba(255,255,255,.14); }
  .ln.now { transform: scale(1.02); color: #fff; }
  #dots { position: absolute; left: 0; top: 36%; display: flex; gap: 1vw; opacity: 0; transition: opacity .4s ease; }
  #dots.on { opacity: 1; }
  .dot { width: 1.25vw; height: 1.25vw; border-radius: 50%; background: rgba(255,255,255,.26); transition: background .3s ease; }
  .dot.on { background: #fff; }
</style>
</head>
<body class="plain">
<canvas id="gl"></canvas>
<aside id="left">
  <div id="frame"><img id="art" alt=""></div>
  <div id="meta">
    <div id="title"></div>
    <div id="artist"></div>
    <div id="bar"><div id="fill"></div></div>
  </div>
</aside>
<section id="view">
  <div id="lyr"></div>
  <div id="dots"><i class="dot"></i><i class="dot"></i><i class="dot"></i></div>
</section>
<script>
  const $ = (id) => document.getElementById(id);
  let shownCover = -1, shownText = "", shownLyrics = "";
  let pos = 0, rate = 0, posAt = performance.now(), dur = 0;
  let lines = [], rows = [], active = -2;
  let bass = 0, smooth = 0, delay = 0;

  new EventSource("/levels").onmessage = (e) => { bass = parseFloat(e.data) || 0; };
  const position = () => pos + (performance.now() - posAt) / 1000 * rate;

  function buildLyrics(data) {
    lines = data;
    active = -2;
    const box = $("lyr");
    box.textContent = "";
    rows = lines.map((line) => {
      const row = document.createElement("div");
      row.className = "ln";
      row.textContent = line.text;
      box.appendChild(row);
      return row;
    });
    document.body.className = lines.length ? "" : "plain";
  }

  function scrollTo(index) {
    const view = $("view").clientHeight;
    const row = index >= 0 ? rows[index] : rows[0];
    const y = view * .37 - row.offsetTop - row.offsetHeight / 2 + (index >= 0 ? 0 : view * .1);
    $("lyr").style.transform = "translate3d(0," + y.toFixed(1) + "px,0)";
  }

  function updateLyrics() {
    if (!lines.length) return;
    const now = position() - delay + .1;
    let index = -1;
    for (let i = 0; i < lines.length; i++) { if (lines[i].t <= now) index = i; else break; }
    if (index !== active) {
      active = index;
      rows.forEach((r, i) => {
        r.classList.toggle("now", i === index);
        r.classList.toggle("past", i < index);
      });
      scrollTo(index);
    }
    let gapFrom = null, gapTo = null;
    if (index < 0 && lines[0].t > 3.5) { gapFrom = 0; gapTo = lines[0].t; }
    else if (index >= 0 && index + 1 < lines.length && lines[index + 1].t - lines[index].end > 4
             && now > lines[index].end) { gapFrom = lines[index].end; gapTo = lines[index + 1].t; }
    const dots = $("dots");
    dots.classList.toggle("on", gapFrom !== null);
    $("lyr").style.opacity = gapFrom !== null ? .35 : 1;
    if (gapFrom !== null) {
      const f = (now - gapFrom) / (gapTo - gapFrom);
      [...dots.children].forEach((d, k) => d.classList.toggle("on", f > k / 3));
    }
  }

  (function startBackground() {
    const canvas = $("gl");
    const gl = canvas.getContext("webgl", { antialias: false, alpha: false, powerPreference: "low-power" });
    if (!gl) { canvas.style.background = "#14141c"; canvas.style.opacity = 1; return; }
    canvas.width = 480; canvas.height = 270;
    const compile = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
    };
    const vs = compile(gl.VERTEX_SHADER, "attribute vec2 p; void main(){ gl_Position = vec4(p, 0., 1.); }");
    const fs = compile(gl.FRAGMENT_SHADER, `
      precision highp float;
      uniform vec2 r; uniform float t, bass, fade; uniform sampler2D A, B;
      vec2 flow(vec2 p, float s, float amt){
        p -= .5;
        float c = cos(s * .33), n = sin(s * .33);
        p = mat2(c, -n, n, c) * p;
        for (int i = 0; i < 3; i++) {
          float k = float(i) + 1.;
          p += amt / k * vec2(sin(p.y * 3.2 * k + s * (.9 + .2 * k) + k * 1.7),
                              cos(p.x * 2.8 * k - s * (.7 + .25 * k) + k * 2.9));
        }
        return p * .62 + .5;
      }
      vec3 art(sampler2D tx, vec2 uv, float T, float amt){
        return (texture2D(tx, flow(uv, T, amt)).rgb
              + texture2D(tx, flow(uv + .2, T * .7 + 4., amt * 1.2)).rgb
              + texture2D(tx, flow(uv - .15, T * 1.2 + 9., amt * .9)).rgb) / 3.;
      }
      float grain(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
      void main(){
        vec2 uv = gl_FragCoord.xy / r;
        vec2 q = vec2(uv.x * r.x / r.y * .62 + .0, uv.y);
        float T = t * .24;
        float amt = .23 + .045 * bass;
        vec3 col = mix(art(B, q, T, amt), art(A, q, T, amt), fade);
        float l = dot(col, vec3(.3, .59, .11));
        col = mix(vec3(l), col, 1.5);
        col = max(col, 0.);
        col *= .66 + .09 * bass;
        col *= mix(1., .72, smoothstep(.36, .7, uv.x));
        col *= mix(.62, 1., smoothstep(1.2, .35, length(uv - .5) * 1.5));
        col += (grain(gl_FragCoord.xy + fract(t)) - .5) / 90.;
        gl_FragColor = vec4(col, 1.);
      }`);
    if (!vs || !fs) { canvas.style.background = "#14141c"; canvas.style.opacity = 1; return; }
    const prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.vertexAttribPointer(gl.getAttribLocation(prog, "p"), 2, gl.FLOAT, false, 0, 0);
    gl.enableVertexAttribArray(gl.getAttribLocation(prog, "p"));
    const u = (n) => gl.getUniformLocation(prog, n);
    gl.uniform2f(u("r"), canvas.width, canvas.height);
    gl.viewport(0, 0, canvas.width, canvas.height);
    const uT = u("t"), uBass = u("bass"), uFade = u("fade");
    gl.uniform1i(u("A"), 0); gl.uniform1i(u("B"), 1);

    const makeTexture = (unit) => {
      const tx = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, tx);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.MIRRORED_REPEAT);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.MIRRORED_REPEAT);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([30, 30, 44, 255]));
      return tx;
    };
    let textures = [makeTexture(0), makeTexture(1)];
    let fade = 1;
    const paint = document.createElement("canvas");
    paint.width = paint.height = 256;
    const ctx = paint.getContext("2d");

    window.setArtwork = (img) => {
      ctx.filter = "blur(10px) saturate(1.25)";
      ctx.drawImage(img, -16, -16, 288, 288);
      textures = [textures[1], textures[0]];
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, textures[0]);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, paint);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures[1]);
      fade = 0;
    };

    let last = 0;
    function frame(ms) {
      requestAnimationFrame(frame);
      updateLyrics();
      if (dur > 0) $("fill").style.transform = "scaleX(" + Math.min(1, Math.max(0, (position() - delay) / dur)).toFixed(4) + ")";
      if (ms - last < 30) return;
      last = ms;
      smooth += (bass - smooth) * (bass > smooth ? .4 : .08);
      fade = Math.min(1, fade + .022);
      gl.uniform1f(uT, ms / 1000);
      gl.uniform1f(uBass, smooth);
      gl.uniform1f(uFade, fade);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      $("frame").style.transform = "scale(" + (1 + .012 * smooth).toFixed(4) + ")";
    }
    requestAnimationFrame(frame);
    canvas.style.opacity = 1;
  })();

  function setCover(version) {
    const url = "/cover?v=" + version;
    const probe = new Image();
    probe.onload = () => {
      if (version !== shownCover) return;
      $("art").src = url; $("art").style.opacity = 1;
      if (window.setArtwork) window.setArtwork(probe);
    };
    probe.onerror = () => { if (version === shownCover) $("art").style.opacity = 0; };
    probe.src = url;
  }

  async function tick() {
    try {
      const s = await (await fetch("/state", { cache: "no-store" })).json();
      const key = s.title + "|" + s.artist;
      if (key !== shownText) {
        shownText = key;
        $("title").textContent = s.title;
        $("artist").textContent = s.artist;
      }
      if (s.cover !== shownCover) { shownCover = s.cover; setCover(s.cover); }
      if (s.rate !== rate || Math.abs(s.pos - position()) > .22) {
        pos = s.pos; rate = s.rate; posAt = performance.now();
      }
      dur = s.dur;
      delay = s.delay;
      if (s.lyrics !== shownLyrics) {
        shownLyrics = s.lyrics;
        buildLyrics(await (await fetch("/lyrics", { cache: "no-store" })).json());
      }
    } catch (e) { /* the service restarts with the receiver; keep polling */ }
    setTimeout(tick, 500);
  }
  tick();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream_levels(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                with levels_cond:
                    levels_cond.wait(timeout=1.0)
                    line = level_value
                self.wfile.write(f"data: {line}\n\n".encode())
        except OSError:
            return

    def current_state(self):
        with lock:
            pos = playback["elapsed"]
            rate = playback["rate"]
            if rate > 0:
                pos += (time.monotonic() - playback["at"]) * rate
            if playback["duration"]:
                pos = min(pos, playback["duration"])
            out = dict(state, pos=round(pos, 3), rate=rate, dur=playback["duration"], lyrics=lyrics["key"], delay=lyrics_delay())
        if not DEMO and not output_running():
            out["rate"] = 0  # the output stopped, whatever the last report said
        return out

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(200, "text/html; charset=utf-8", PAGE.encode())
        elif path == "/state":
            self.send(200, "application/json; charset=utf-8", json.dumps(self.current_state(), ensure_ascii=False).encode())
        elif path == "/lyrics":
            with lock:
                body = json.dumps(lyrics["lines"], ensure_ascii=False).encode()
            self.send(200, "application/json; charset=utf-8", body)
        elif path == "/calibrate":
            self.send(200, "application/json; charset=utf-8", json.dumps(measure_delay()).encode())
        elif path == "/levels":
            self.stream_levels()
        elif path.startswith("/font/"):
            name = FONTS.get(path.rsplit("/", 1)[-1])
            try:
                with open(os.path.join(FONT_DIR, name or ""), "rb") as f:
                    self.send(200, "font/otf", f.read())
            except OSError:
                self.send(404, "text/plain", b"")
        elif path == "/cover":
            with lock:
                data = cover["bytes"]
            if not data:
                self.send(404, "text/plain", b"")
            else:
                ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
                self.send(200, ctype, data)
        else:
            self.send(404, "text/plain", b"")


class Server(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if not DEMO:
        load_state()
    threading.Thread(target=metadata_reader, daemon=True).start()
    if DEMO:
        threading.Thread(target=demo_loop, daemon=True).start()
    else:
        threading.Thread(target=capture_loop, daemon=True).start()
        threading.Thread(target=lyrics_worker, daemon=True).start()
        threading.Thread(target=state_saver, daemon=True).start()
    Server(("127.0.0.1", PORT), Handler).serve_forever()
