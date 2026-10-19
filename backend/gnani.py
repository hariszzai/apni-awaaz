"""Async client for the Gnani speech APIs: Prisma STT, Timbre TTS and voice cloning."""
import asyncio
import base64
import io
import json
import logging
import time
import wave
from collections.abc import AsyncIterator

import httpx
import websockets

BASE_URL = "https://api.vachana.ai"
STT_MAX_SECONDS = 60  # Prisma REST limit; longer audio needs the streaming endpoint

# Prisma realtime STT (tested 19 Oct): it splits speech into segments on silence and
# transcribes each one while the user is still talking. There is no end-of-audio message,
# so the last segment only closes after silence: we send 400 ms of it, with the silence
# threshold lowered to 300 ms. The final text then arrives ~0.4-0.55 s after release
# (REST: 1-1.6 s). An open stream does not block other calls on the same key.
STREAM_URL = "wss://api.vachana.ai/stt/v3/stream"
STREAM_RATE = 16000
STREAM_FRAME_BYTES = 1024  # the API requires exactly 512 samples per frame
STREAM_FRAME_SECONDS = STREAM_FRAME_BYTES / 2 / STREAM_RATE
STREAM_MIN_SILENCE_MS = 300
STREAM_TAIL_SILENCE_SECONDS = 0.4
STREAM_SETTLE_TIMEOUT = 3.0

log = logging.getLogger("apni_awaaz.gnani")

AUDIO_CONFIG = {
    "sample_rate": 44100,
    "num_channels": 1,
    "sample_width": 2,
    "encoding": "linear_pcm",
    "container": "wav",
}

# Timbre v3.0 audio tags. They are ignored by cloned voices (vachana-vc-v1), see SRS section 4.
EMOTION_TAGS = (
    "excited", "laugh", "giggle", "whispers", "sigh", "exhales", "slow", "pauses",
    "shouting", "coughs", "sniffles", "yawns", "clears throat",
)


class GnaniError(RuntimeError):
    pass


class DailyLimitReached(GnaniError):
    """The demo's daily cap on Gnani calls is used up (protects the programme credits)."""


def parse_embedding(data: dict) -> dict:
    """Pull the speaker embedding out of an embeddings API response."""
    # Live API nests it under data.voice_clone_embedding; the docs show it top-level
    emb = data.get("data", {}).get("voice_clone_embedding", data)
    try:
        return {k: emb[k] for k in ("embedding", "shape", "dtype")}
    except KeyError as e:
        raise GnaniError(f"Embedding response has no {e} field: {str(data)[:200]}") from None


def pcm_from_wav(chunk: bytes) -> bytes:
    """Strip the WAV header if a chunk has one. Cloned SSE chunks each carry their own header."""
    if not chunk.startswith(b"RIFF"):
        return chunk
    i = chunk.find(b"data", 12)
    if i < 0:
        raise GnaniError("WAV audio has no data section")
    return chunk[i + 8:]


def wav_from_pcm(pcm: bytes, sample_rate: int = AUDIO_CONFIG["sample_rate"]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class SttStream:
    """One push-to-talk turn through Prisma realtime STT.

    Feed 16 kHz 16-bit mono PCM with send() as it is recorded (any chunk size), then
    await finish() for the full transcript. Audio sent before the connection is ready
    is buffered and flushed in one go, which the API accepts.
    """

    def __init__(self, api_key: str, language_code: str):
        self._headers = {
            "x-api-key-id": api_key,
            "lang_code": language_code,
            "x-sample-rate": str(STREAM_RATE),
            "x-min-silence-ms": str(STREAM_MIN_SILENCE_MS),
        }
        self._pending = bytearray()
        self._frames: asyncio.Queue[tuple[bytes, bool] | None] = asyncio.Queue()
        self._segments: dict[int, str] = {}
        self._speech_starts = 0
        self._transcripts = 0
        self._changed = asyncio.Event()
        self._closed = False
        self.error: Exception | None = None
        # Set as soon as the transcript is complete. Closing the socket afterwards takes
        # ~1 s (Prisma is slow to answer the close handshake), so that happens in the background.
        self._done = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())

    def send(self, pcm: bytes):
        if self._closed:
            return
        self._pending.extend(pcm)
        while len(self._pending) >= STREAM_FRAME_BYTES:
            self._frames.put_nowait((bytes(self._pending[:STREAM_FRAME_BYTES]), False))
            del self._pending[:STREAM_FRAME_BYTES]

    async def finish(self) -> str:
        """Close the last segment with silence and return the whole transcript."""
        if not self._closed:
            self._closed = True
            if self._pending:
                self._frames.put_nowait((bytes(self._pending).ljust(STREAM_FRAME_BYTES, b"\0"), False))
                self._pending.clear()
            # Silence goes out in real time: sending it in a burst made the VAD slower to close
            for _ in range(round(STREAM_TAIL_SILENCE_SECONDS / STREAM_FRAME_SECONDS)):
                self._frames.put_nowait((b"\0" * STREAM_FRAME_BYTES, True))
            self._frames.put_nowait(None)
        await self._done
        if self.error:
            raise GnaniError(f"Prisma stream failed: {self.error}")
        return " ".join(self._segments[k] for k in sorted(self._segments)).strip()

    def cancel(self):
        self._closed = True
        self._task.cancel()

    def _complete(self):
        if not self._done.done():
            self._done.set_result(None)

    async def _run(self):
        try:
            async with websockets.connect(
                STREAM_URL, additional_headers=self._headers, max_size=None, open_timeout=10
            ) as ws:
                reader = asyncio.create_task(self._read(ws))
                try:
                    paced_start = None
                    paced_count = 0
                    while (item := await self._frames.get()) is not None:
                        frame, paced = item
                        await ws.send(frame)
                        if paced:
                            # Pace by the clock: on Windows each short sleep overshoots by ~15 ms
                            paced_start = paced_start or time.perf_counter()
                            paced_count += 1
                            delay = paced_start + paced_count * STREAM_FRAME_SECONDS - time.perf_counter()
                            if delay > 0:
                                await asyncio.sleep(delay)
                        if reader.done():
                            break
                    await asyncio.wait_for(self._settled(), STREAM_SETTLE_TIMEOUT)
                except TimeoutError:
                    log.warning("Prisma stream: last segment never came back")
                finally:
                    reader.cancel()
                    self._complete()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection or protocol failure; the caller falls back to REST
            self.error = e
        finally:
            self._complete()

    async def _settled(self):
        # Every segment the VAD started must have its transcript. The short grace period
        # catches a speech_start for the final words that is still in flight.
        await asyncio.sleep(0.15)
        while self._speech_starts > self._transcripts:
            self._changed.clear()
            await self._changed.wait()

    async def _read(self, ws):
        async for message in ws:
            data = json.loads(message)
            kind = data.get("type")
            if kind == "speech_start":
                self._speech_starts += 1
            elif kind == "transcript":
                self._transcripts += 1
                text = (data.get("text") or "").strip()
                if text:
                    self._segments[data.get("segment_index", len(self._segments))] = text
            elif kind == "error":
                log.warning("Prisma stream error: %s", data.get("message"))
            self._changed.set()


def _check(response: httpx.Response, what: str) -> httpx.Response:
    if response.status_code != 200:
        raise GnaniError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")
    return response


def _cloned_body(text: str, embedding: dict) -> dict:
    return {"text": text, "model": "vachana-vc-v1", "audio_config": AUDIO_CONFIG, "speaker_embedding": embedding}


def _stock_body(text: str, voice: str, language_code: str, model: str) -> dict:
    return {
        "text": text,
        "voice": voice,
        "model": model,
        "language": language_code,
        "speed": 1.0,
        "audio_config": AUDIO_CONFIG,
    }


class GnaniClient:
    """Calls are queued: the API allows only one active request per key (HTTP 429 otherwise)."""

    def __init__(self, api_key: str, timeout: float = 120, daily_limit: int = 0):
        self._api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=BASE_URL, headers={"X-API-Key-ID": api_key}, timeout=timeout
        )
        self._slot = asyncio.Lock()
        self.daily_limit = daily_limit  # 0 = no cap
        self.calls_today = 0
        self._day = time.strftime("%Y-%m-%d")

    def _spend(self):
        """Count one credit-using call; refuse once today's cap is reached."""
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.calls_today = today, 0
        if self.daily_limit and self.calls_today >= self.daily_limit:
            raise DailyLimitReached("Daily demo limit reached")
        self.calls_today += 1

    def stream_stt(self, language_code: str) -> SttStream:
        """Start a realtime STT stream. It does not take the one-request slot (tested)."""
        self._spend()
        return SttStream(self._api_key, language_code)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def transcribe(self, audio: bytes, filename: str, language_code: str) -> str:
        """Prisma STT (REST, audio up to 60 s)."""
        self._spend()
        async with self._slot:
            r = await self._http.post(
                "/stt/v3",
                files={"audio_file": (filename, audio)},
                data={"language_code": language_code},
            )
        data = _check(r, "Prisma STT").json()
        if data.get("success") is False:
            raise GnaniError(f"Prisma STT failed: {str(data)[:300]}")
        return (data.get("transcript") or "").strip()

    async def create_embedding(self, audio: bytes, filename: str) -> dict:
        """Voice cloning step 1: a speaker embedding from 5-30 s of clear speech."""
        self._spend()
        async with self._slot:
            r = await self._http.post(
                "/api/v1/tts/voice-clone/embeddings", files={"audio_file": (filename, audio)}
            )
        return parse_embedding(_check(r, "Voice clone embedding").json())

    async def cloned_tts(self, text: str, embedding: dict) -> bytes:
        """Speak text in the cloned voice. Returns WAV bytes.

        Cloned voices don't really stream: SSE and WebSocket both deliver all audio at the
        end (~2-3 s, mostly fixed cost), so REST is just as fast.
        """
        self._spend()
        async with self._slot:
            r = await self._http.post("/api/v1/tts/inference", json=_cloned_body(text, embedding))
        return _check(r, "Cloned TTS").content

    async def stock_tts(self, text: str, voice: str, language_code: str, model: str = "timbre-v3.0") -> bytes:
        """Speak text in a stock Timbre voice (emotion tags work on v3.0). Returns WAV bytes."""
        self._spend()
        async with self._slot:
            r = await self._http.post("/api/v1/tts/inference", json=_stock_body(text, voice, language_code, model))
        return _check(r, "Timbre TTS").content

    async def stream_stock_tts(
        self, text: str, voice: str, language_code: str, model: str = "timbre-v3.0"
    ) -> AsyncIterator[bytes]:
        """Stock Timbre over SSE. Yields raw PCM chunks; the first arrives in ~0.25 s."""
        body = _stock_body(text, voice, language_code, model)
        self._spend()
        async with self._slot, self._http.stream("POST", "/api/v1/tts/sse", json=body) as r:
            if r.status_code != 200:
                await r.aread()
                _check(r, "Timbre TTS stream")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:])
                if "error" in data:
                    raise GnaniError(f"Timbre TTS stream failed: {str(data)[:300]}")
                if data.get("audio"):
                    yield pcm_from_wav(base64.b64decode(data["audio"]))
                if data.get("is_final"):
                    return
