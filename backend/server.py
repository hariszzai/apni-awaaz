"""Apni Awaaz web server: voice setup, voice previews and two-person conversation rooms.

Run from the project folder:
  python -m backend.server             laptop only, http://localhost:8000
  python -m backend.server --https     phones too, https://<laptop IP>:8443 (same Wi-Fi/hotspot)
  python -m backend.server --public    on a host: listens on $PORT (default 7860) behind its HTTPS proxy
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ROOT, load_settings
from .gnani import DailyLimitReached, GnaniClient, GnaniError
from .languages import LANGUAGES
from .limits import RateLimiter
from .rooms import RoomManager
from .translator import Translator
from .voices import (
    EXPRESSIVE_PREFIX,
    PREVIEW_TEXT,
    STOCK_VOICES,
    LegacyVoices,
    PreviewCache,
    VoiceSigner,
    voice_id,
)

STATIC = ROOT / "static"
MAX_TURN_SECONDS = 55  # Prisma REST takes up to 60 s; leave a margin (SRS FR-7)
MAX_VOICE_BYTES = 10 * 1024 * 1024
LIMIT_MESSAGE = "The demo has reached today's limit. Please try again tomorrow."

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("apni_awaaz")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    async with GnaniClient(settings.gnani_api_key, daily_limit=settings.gnani_daily_limit) as gnani, Translator(
        settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_reasoning_effort
    ) as translator:
        app.state.gnani = gnani
        app.state.signer = VoiceSigner(settings.secret_key)
        app.state.previews = PreviewCache(settings.data_dir / "previews")
        app.state.legacy = LegacyVoices(settings.data_dir / "voices")
        app.state.limiter = RateLimiter()
        app.state.rooms = RoomManager(gnani, translator, app.state.signer, app.state.limiter, settings.stock_voice)
        yield


app = FastAPI(title="Apni Awaaz", lifespan=lifespan)


@app.middleware("http")
async def always_revalidate(request, call_next):
    # Browsers otherwise keep old copies of the page, CSS and JS after an update (phones
    # especially). "no-cache" still caches, but checks first; unchanged files cost a 304.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def visitor(request: Request) -> str:
    # Behind a host's proxy, --public mode makes this the visitor's real address
    return request.client.host if request.client else "unknown"


def check_limit(request: Request, action: str):
    if not app.state.limiter.allow(action, visitor(request)):
        raise HTTPException(429, "Too many requests. Please wait a little and try again.")


def wav(audio: bytes) -> Response:
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config():
    return {
        "languages": LANGUAGES,
        "voices": STOCK_VOICES,
        "max_turn_seconds": MAX_TURN_SECONDS,
    }


@app.post("/api/rooms")
async def create_room(request: Request):
    check_limit(request, "room")
    return {"room_id": app.state.rooms.create()}


@app.post("/api/voice")
async def create_voice(
    request: Request,
    consent: bool = Form(...),
    audio_file: UploadFile = File(...),
):
    """Clone a voice from a recording. The browser keeps the signed result."""
    if not consent:
        raise HTTPException(400, "Consent is required to clone your voice")
    check_limit(request, "clone")
    audio = await audio_file.read(MAX_VOICE_BYTES + 1)
    if len(audio) > MAX_VOICE_BYTES:
        raise HTTPException(413, "Recording is too large")
    try:
        embedding = await app.state.gnani.create_embedding(audio, audio_file.filename or "voice.wav")
    except DailyLimitReached:
        raise HTTPException(503, LIMIT_MESSAGE) from None
    except GnaniError as e:
        log.warning("Voice setup failed: %s", e)
        raise HTTPException(502, "Couldn't create your voice. Try recording again in a quiet place.") from None
    return {"voice": app.state.signer.seal(embedding)}


@app.post("/api/voice/migrate")
async def migrate_voice(user_id: str = Body(..., embed=True)):
    """Hand a voice saved on the server by the first version of the app back to its browser."""
    embedding = app.state.legacy.take(user_id)
    return {"voice": app.state.signer.seal(embedding) if embedding else None}


@app.post("/api/voice/preview")
async def preview_voice(request: Request, voice: dict = Body(...), language: str = Body(...)):
    """A short sentence in the user's cloned voice, in their language."""
    embedding = app.state.signer.open(voice)
    if embedding is None:
        raise HTTPException(400, "This voice isn't valid any more. Please record it again.")
    if language not in PREVIEW_TEXT:
        raise HTTPException(400, "Unsupported language")
    name = voice_id(embedding)
    if cached := app.state.previews.get("clone", name, language):
        return wav(cached)
    check_limit(request, "preview")
    try:
        audio = await app.state.gnani.cloned_tts(PREVIEW_TEXT[language], embedding)
    except DailyLimitReached:
        raise HTTPException(503, LIMIT_MESSAGE) from None
    except GnaniError as e:
        log.warning("Clone preview failed: %s", e)
        raise HTTPException(502, "Couldn't make the preview. Please try again.") from None
    app.state.previews.put("clone", name, language, audio)
    return wav(audio)


@app.post("/api/voice/forget")
async def forget_voice(voice: dict = Body(..., embed=True)):
    """Delete the cached preview clips of a voice the user is deleting."""
    embedding = app.state.signer.open(voice)
    removed = app.state.previews.forget_clone(voice_id(embedding)) if embedding else 0
    return {"removed": removed}


@app.get("/api/voices/{name}/preview")
async def preview_stock_voice(request: Request, name: str, language: str):
    """A stock Expressive voice saying the preview sentence, with some excitement."""
    if name not in STOCK_VOICES or language not in PREVIEW_TEXT:
        raise HTTPException(404, "Unknown voice or language")
    if cached := app.state.previews.get("stock", name, language):
        return wav(cached)
    check_limit(request, "preview")
    try:
        audio = await app.state.gnani.stock_tts(EXPRESSIVE_PREFIX + PREVIEW_TEXT[language], name, language)
    except DailyLimitReached:
        raise HTTPException(503, LIMIT_MESSAGE) from None
    except GnaniError as e:
        log.warning("Voice preview failed: %s", e)
        raise HTTPException(502, "Couldn't make the preview. Please try again.") from None
    app.state.previews.put("stock", name, language, audio)
    return wav(audio)


@app.websocket("/ws/{room_id}")
async def room_socket(ws: WebSocket, room_id: str):
    await app.state.rooms.handle(ws, room_id)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import argparse

    import uvicorn

    from .certs import ensure_cert

    parser = argparse.ArgumentParser(description="Run the Apni Awaaz server")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--https", action="store_true", help="serve over HTTPS so phones can use the microphone")
    mode.add_argument("--public", action="store_true", help="run on a host, behind its HTTPS proxy")
    parser.add_argument("--port", type=int, help="default 8000, 8443 with --https, $PORT or 7860 with --public")
    args = parser.parse_args()

    if args.public:
        # The host terminates HTTPS and forwards requests; trust its headers for the visitor's IP
        port = args.port or int(os.environ.get("PORT", "7860"))
        uvicorn.run("backend.server:app", host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
        return

    if not args.https:
        port = args.port or 8000
        print(f"\n  Apni Awaaz on this laptop: http://localhost:{port}\n  (Phones need --https)\n")
        uvicorn.run("backend.server:app", host="127.0.0.1", port=port)
        return

    port = args.port or 8443
    cert, key, ip = ensure_cert(ROOT / "certs")
    print(f"\n  Apni Awaaz over HTTPS")
    print(f"  Laptop:  https://localhost:{port}")
    if ip:
        print(f"  Phones:  https://{ip}:{port}   (same Wi-Fi or hotspot as this laptop)")
    else:
        print("  Phones:  no network found. Connect the laptop to Wi-Fi or a hotspot.")
    print("  Your browser will warn the certificate isn't trusted: tap Advanced -> Proceed.")
    print("  If Windows asks whether Python may use the network, allow Private networks.\n")
    uvicorn.run("backend.server:app", host="0.0.0.0", port=port, ssl_certfile=str(cert), ssl_keyfile=str(key))


if __name__ == "__main__":
    main()
