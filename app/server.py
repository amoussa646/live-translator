"""Live Translator — local engine + web UI.

Speech -> text: Voxtral Realtime (MLX).  Text -> translation: TranslateGemma 4B (MLX).
Models load from ./models only; Hugging Face is forced offline. Two listeners:
  http://localhost:8787            this Mac (no certificate warning)
  https://<lan-ip>:8788/?key=...   phones/laptops on the same Wi-Fi (self-signed cert, key required)
"""
import os
import sys

for _k, _v in {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false"}.items():
    os.environ.setdefault(_k, _v)

import asyncio
import contextlib
import hmac
import itertools
import json
import logging
import secrets
import signal
import socket
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
from segmenter import Segmenter, detect_lang  # noqa: E402
from translator import LANGS, Translator  # noqa: E402

VOX_DIR = ROOT / "models" / "voxtral-realtime-6bit"
MT_DIR = ROOT / "models" / "translategemma-4b-it-4bit"
CERT_DIR = ROOT / "certs"
HTTP_PORT = int(os.environ.get("LT_PORT", "8787"))
LAN_PORT = int(os.environ.get("LT_LAN_PORT", "8788"))
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("whisperlivekit", "httpx", "uvicorn.access"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("live-translator")

# MLX GPU streams are per-thread: every speech-model call must happen on the thread that
# loaded it, and every translation call on the translator's thread.
ASR_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
MT_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt")
STATE = {"engine": None, "mt": None}
WATCHERS: set = set()
BACKLOG: deque = deque(maxlen=600)
SESSION_SEQ = itertools.count(1)
LAN_URL = None


def _token() -> str:
    CERT_DIR.mkdir(mode=0o700, exist_ok=True)
    CERT_DIR.chmod(0o700)
    f = CERT_DIR / "access-key.txt"
    if not f.exists() or "--new-key" in sys.argv:  # ./run.sh --new-key logs every device out
        f.write_text(secrets.token_urlsafe(16))  # 128-bit key
        f.chmod(0o600)
    return f.read_text().strip()


TOKEN = _token()


def load_models():
    from whisperlivekit import TranscriptionEngine
    t = time.time()
    log.info("Loading speech model (Voxtral Realtime) from %s", VOX_DIR)
    STATE["engine"] = ASR_EXEC.submit(lambda: TranscriptionEngine(
        backend="voxtral-mlx", model_path=str(VOX_DIR), lan="auto", vac=True,
        pcm_input=True, target_language="", warmup_file=None, log_level="WARNING")).result()
    log.info("Loading translation model (TranslateGemma 4B) from %s", MT_DIR)
    STATE["mt"] = MT_EXEC.submit(Translator, str(MT_DIR)).result()
    log.info("Models ready in %.1fs", time.time() - t)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# Security
#  * this Mac: plain http bound to 127.0.0.1 only
#  * other devices: https (self-signed, this Mac's own cert) + 128-bit access key,
#    exchanged for an HttpOnly/Secure/SameSite=Strict cookie, then stripped from the URL
#  * only private (home/office network) addresses are accepted; public IPs are refused
#  * Host + Origin checks stop DNS-rebinding and cross-site WebSocket hijacking
#  * 10 wrong keys from one address -> locked out for 10 minutes
#  * strict CSP, no third-party anything; transcripts are never written to disk or logs
# ---------------------------------------------------------------------------
FAILS: dict = {}
MAX_FAILS, FAIL_WINDOW = 10, 600
MAX_SESSIONS = int(os.environ.get("LT_MAX_SESSIONS", "4"))
MAX_WATCHERS = 20
ACTIVE = {"n": 0}
ALLOWED_HOSTS: set = {"localhost", "127.0.0.1", "[::1]", "::1"}
SEC_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self' ws: wss:; worker-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'none'; object-src 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=(), display-capture=(self)",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _client(conn) -> str:
    return conn.client.host if conn.client else ""


def _private(ip: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def _host_ok(conn) -> bool:
    host = (conn.headers.get("host") or "").lower()
    if not host.endswith("]"):
        host = host.rsplit(":", 1)[0]
    return host in ALLOWED_HOSTS


def _origin_ok(conn) -> bool:
    from urllib.parse import urlsplit
    origin = conn.headers.get("origin")
    if origin is None:  # non-browser client: only scripts on this Mac
        return _client(conn) in LOCAL_HOSTS
    o = urlsplit(origin)
    return (o.hostname or "") in ALLOWED_HOSTS and o.netloc == conn.headers.get("host")


def _blocked(ip: str) -> bool:
    rec = FAILS.get(ip)
    if not rec:
        return False
    if time.time() - rec[1] > FAIL_WINDOW:
        FAILS.pop(ip, None)
        return False
    return rec[0] >= MAX_FAILS


def _key_ok(key: str) -> bool:
    return bool(key) and hmac.compare_digest(key.encode(), TOKEN.encode())


def _authorized(conn) -> bool:
    ip = _client(conn)
    if ip in LOCAL_HOSTS:
        return True
    if not _private(ip) or _blocked(ip):
        return False
    key = conn.query_params.get("key") or conn.cookies.get("lt_key") or ""
    if _key_ok(key):
        return True
    if key:
        rec = FAILS.setdefault(ip, [0, time.time()])
        rec[0] += 1
        if rec[0] == MAX_FAILS:
            log.warning("locked out %s for %d s after %d wrong keys", ip, FAIL_WINDOW, MAX_FAILS)
    return False


@app.middleware("http")
async def guard(request: Request, call_next):
    if not _host_ok(request):
        return PlainTextResponse("Unknown host.", status_code=400, headers=SEC_HEADERS)
    if not _authorized(request):
        return PlainTextResponse("Locked. Open the full link (with ?key=...) shown on the Mac.",
                                 status_code=401, headers=SEC_HEADERS)
    if _key_ok(request.query_params.get("key", "")):
        # Trade the key in the URL for a cookie, so it doesn't stay in history or on screen.
        resp = RedirectResponse(request.url.path or "/", status_code=303)
        resp.set_cookie("lt_key", TOKEN, httponly=True, samesite="strict", secure=True, max_age=30 * 86400)
    else:
        resp = await call_next(request)
    resp.headers.update(SEC_HEADERS)
    return resp


async def _ws_gate(ws: WebSocket) -> bool:
    if _host_ok(ws) and _origin_ok(ws) and _authorized(ws):
        return True
    log.warning("refused websocket from %s (origin=%s)", _client(ws), ws.headers.get("origin"))
    await ws.close(code=4401)
    return False


@app.get("/api/status")
async def api_status(request: Request):
    local = (request.client.host if request.client else "") in LOCAL_HOSTS
    return {"ready": STATE["mt"] is not None, "languages": LANGS, "lan_url": LAN_URL if local else None}


async def broadcast(obj: dict):
    if obj["type"] in ("hello", "sentence", "translation"):
        BACKLOG.append(obj)
    if not WATCHERS:
        return
    data = json.dumps(obj, ensure_ascii=False)
    for w in list(WATCHERS):
        try:
            await w.send_text(data)
        except Exception:
            WATCHERS.discard(w)


@app.websocket("/ws/watch")
async def ws_watch(ws: WebSocket):
    """Read-only view of every live session (e.g. the Mac listens, phones read)."""
    if not await _ws_gate(ws):
        return
    await ws.accept()
    if len(WATCHERS) >= MAX_WATCHERS:
        await ws.close(code=1013)
        return
    for obj in list(BACKLOG):
        await ws.send_text(json.dumps(obj, ensure_ascii=False))
    WATCHERS.add(ws)
    try:
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass
    finally:
        WATCHERS.discard(ws)


@app.websocket("/ws")
async def ws_session(ws: WebSocket):
    """One audio stream in (16 kHz mono s16le PCM), transcript + translations out."""
    if not await _ws_gate(ws):
        return
    await ws.accept()
    if ACTIVE["n"] >= MAX_SESSIONS:
        await ws.send_text(json.dumps({"type": "error", "error": f"Busy: {MAX_SESSIONS} sessions already running."}))
        await ws.close(code=1013)
        return
    ACTIVE["n"] += 1
    try:
        await _session(ws)
    finally:
        ACTIVE["n"] -= 1


async def _session(ws: WebSocket):
    from whisperlivekit import AudioProcessor

    sid = next(SESSION_SEQ)
    if ws.query_params.get("source", "auto") not in (*LANGS, "auto") or \
            ws.query_params.get("target", "ru") not in (*LANGS, "none"):
        await ws.close(code=4400)
        return
    cfg = {"source": ws.query_params.get("source", "auto"), "target": ws.query_params.get("target", "ru")}
    lock = asyncio.Lock()
    alive = {"v": True}
    loop = asyncio.get_running_loop()

    async def send(obj: dict, share: bool = True):
        obj["session"] = sid
        if share:
            await broadcast(obj)
        if not alive["v"]:
            return
        try:
            async with lock:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            alive["v"] = False

    ap = await loop.run_in_executor(ASR_EXEC, lambda: AudioProcessor(transcription_engine=STATE["engine"], pcm_input=True))
    results = await ap.create_tasks()
    seg = Segmenter()
    queue: asyncio.Queue = asyncio.Queue()
    snap = {"committed": "", "buffer": "", "t": 0.0}
    counter = {"id": 0, "prev_end": 0.0}

    async def emit(sentences):
        for s in sentences:
            counter["id"] += 1
            src = cfg["source"] if cfg["source"] != "auto" else detect_lang(s)
            start = counter["prev_end"]
            end = max(snap["t"], start)
            counter["prev_end"] = end
            await send({"type": "sentence", "id": counter["id"], "text": s, "lang": src, "start": start, "end": end})
            await queue.put((counter["id"], s, src, time.monotonic()))

    async def push_live():
        await send({"type": "asr", "pending": seg.pending(), "buffer": snap["buffer"], "t": snap["t"]})

    async def consume():
        async for fd in results:
            if fd.status == "error":
                await send({"type": "error", "error": fd.error or "speech engine error"})
                return
            lines = [ln for ln in fd.lines if ln.text and ln.speaker != -2]
            snap["committed"] = "\n".join(ln.text.strip() for ln in lines)
            snap["buffer"] = (fd.buffer_transcription or "").strip()
            if lines and lines[-1].end is not None:
                snap["t"] = float(lines[-1].end)
            await emit(seg.update(snap["committed"], bool(snap["buffer"])))
            await push_live()

    async def ticker():
        while True:
            await asyncio.sleep(0.4)
            out = seg.update(snap["committed"], bool(snap["buffer"]))
            if out:
                await emit(out)
                await push_live()

    async def translate_worker():
        while True:
            item = await queue.get()
            if item is None:
                return
            n, text, src, t_sentence = item
            tgt = cfg["target"]
            if tgt == "none":
                await send({"type": "translation", "id": n, "text": None, "target": tgt})
                continue
            t0 = time.monotonic()
            try:
                out = await loop.run_in_executor(MT_EXEC, STATE["mt"].translate, text, src, tgt)
            except Exception as e:  # keep the session alive
                log.exception("translation failed")
                out = f"[translation error: {e}]"
            await send({"type": "translation", "id": n, "text": out, "target": tgt,
                        "ms": int((time.monotonic() - t0) * 1000),
                        "wait_ms": int((t0 - t_sentence) * 1000)})

    tasks = [asyncio.create_task(consume()), asyncio.create_task(ticker()), asyncio.create_task(translate_worker())]
    consumer, tick, worker = tasks
    await send({"type": "hello", **cfg})
    log.info("session %d started (%s -> %s) from %s", sid, cfg["source"], cfg["target"], ws.client.host if ws.client else "?")
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                with contextlib.suppress(ValueError):
                    data = json.loads(msg["text"])
                    if data.get("type") == "config":
                        if data.get("source") in (*LANGS, "auto"):
                            cfg["source"] = data["source"]
                        if data.get("target") in (*LANGS, "none"):
                            cfg["target"] = data["target"]
                        log.info("session %d now %s -> %s", sid, cfg["source"], cfg["target"])
                continue
            chunk = msg.get("bytes")
            if chunk is None:
                continue
            await ap.process_audio(chunk)
            if not chunk:  # end of audio: drain ASR, flush last sentence, finish translations
                await consumer
                tick.cancel()
                await emit(seg.flush())
                await queue.put(None)
                await worker
                await send({"type": "done"})
                break
    except Exception:
        log.exception("session %d error", sid)
    finally:
        alive["v"] = False
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ap.cleanup()
        log.info("session %d closed", sid)


app.mount("/", StaticFiles(directory=APP_DIR / "web", html=True), name="web")


# ---------------------------------------------------------------------------

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the Wi-Fi interface
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def ensure_cert(ip: str):
    crt, key, tag = CERT_DIR / "lan.crt", CERT_DIR / "lan.key", CERT_DIR / "cert-ip.txt"
    if crt.exists() and key.exists() and tag.exists() and tag.read_text().strip() == ip:
        return crt, key
    san = f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost,DNS:{socket.gethostname()}"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
                    "-keyout", str(key), "-out", str(crt), "-subj", "/CN=Live Translator (local)",
                    "-addext", san], check=True, capture_output=True)
    key.chmod(0o600)
    tag.write_text(ip)
    return crt, key


class _Server(uvicorn.Server):
    """Two servers share one loop; we handle Ctrl+C ourselves."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self):
        pass


async def serve():
    global LAN_URL
    common = dict(lifespan="off", log_level="warning", ws_max_size=1024 * 1024, server_header=False)
    servers = [_Server(uvicorn.Config(app, host="127.0.0.1", port=HTTP_PORT, **common))]
    ip = lan_ip()
    if ip and os.environ.get("LT_LAN", "1") == "1":
        crt, key = ensure_cert(ip)
        ALLOWED_HOSTS.update({ip, socket.gethostname().lower()})
        LAN_URL = f"https://{ip}:{LAN_PORT}/?key={TOKEN}"
        servers.append(_Server(uvicorn.Config(app, host="0.0.0.0", port=LAN_PORT, ssl_certfile=str(crt),
                                              ssl_keyfile=str(key), **common)))
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ASR_EXEC)  # WhisperLiveKit runs the speech model via asyncio.to_thread
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [setattr(s, "should_exit", True) for s in servers])
    print("\n" + "=" * 64)
    print(f"  Live Translator is running (models: {', '.join(LANGS)})")
    print(f"  This Mac:        http://localhost:{HTTP_PORT}")
    if LAN_URL:
        print(f"  Same Wi-Fi:      {LAN_URL}")
        print("                   (accept the certificate warning once; it's this Mac's own cert)")
    print("  Stop: Ctrl+C")
    print("=" * 64 + "\n", flush=True)
    if "--open" in sys.argv:
        subprocess.Popen(["open", f"http://localhost:{HTTP_PORT}"])
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    load_models()
    asyncio.run(serve())
