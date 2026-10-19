"""Two-person conversation rooms over WebSocket.

Protocol (one WebSocket per person, /ws/{room_id}):

  client -> server
    {"type": "join", "user_id", "client_id", "name", "language", "mode",
     "voice" (Expressive voice name), "cloned_voice" (signed, kept by the browser)}   first message
    {"type": "mode", "mode": "my_voice" | "expressive"}
    {"type": "talk_start"}                     button pressed
    <binary>  live 16 kHz 16-bit mono PCM     while the button is held (streamed to Prisma)
    {"type": "talk_end"} | {"type": "talk_cancel"}
    <binary>  outside talk_start/talk_end: one whole turn as a WAV file (Prisma REST)

  server -> client
    {"type": "joined", "you": pid, "mode"}
    {"type": "room", "participants": [...]}
    {"type": "speaking", "speaker", "speaker_name", "on": bool}
    {"type": "status", "turn_id", "speaker", "state": queued|translating|done|failed, ...}
    {"type": "text", "turn_id", "speaker", "transcript", "translation", ...}
    {"type": "audio_start", "turn_id", "speaker_name", "sample_rate"}   listener only
    <binary>  raw PCM chunks, 16-bit mono                                listener only
    {"type": "audio_end", "turn_id"}                                     listener only
    {"type": "error", "message"}
"""
import asyncio
import json
import logging
import re
import secrets
import string
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from .gnani import AUDIO_CONFIG, STREAM_RATE, DailyLimitReached, GnaniClient, GnaniError, SttStream, wav_from_pcm
from .languages import LANGUAGES
from .pipeline import MODES, PipelineError, run_turn
from .translator import Translation, TranslationError, Translator
from .limits import RateLimiter
from .voices import STOCK_VOICES, VoiceSigner, valid_user_id

log = logging.getLogger("apni_awaaz.rooms")

CLIENT_ID = re.compile(r"^[a-f0-9]{16,32}$")
ROOM_ID_CHARS = string.ascii_uppercase + string.digits
ROOM_ID_LENGTH = 6
MAX_PARTICIPANTS = 2
MAX_TURN_BYTES = 5 * 1024 * 1024  # 55 s of 16 kHz WAV is ~1.8 MB
MIN_TALK_BYTES = int(0.3 * STREAM_RATE) * 2  # shorter presses are accidental taps
TOO_MANY_TURNS = "You're talking a lot! Please wait a few minutes before the next turn."
LIMIT_MESSAGE = "The demo has reached today's limit. Please try again tomorrow."
EMPTY_ROOM_TTL = 30 * 60  # keep empty rooms for reconnects (phones drop sockets on screen lock)
MAX_NAME_LENGTH = 30


@dataclass(eq=False)
class Talk:
    """A push-to-talk press in progress: audio goes to Prisma live, and is kept for a REST fallback."""
    stream: SttStream
    pcm: bytearray = field(default_factory=bytearray)


@dataclass(eq=False)
class Participant:
    ws: WebSocket
    pid: str
    user_id: str
    client_id: str  # one per browser tab, so a reload reconnects but a second tab is a second person
    name: str
    language: str
    mode: str
    voice: str
    embedding: dict | None
    connected: bool = True
    talk: Talk | None = None
    ip: str = ""

    def public(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "language": self.language,
            "language_name": LANGUAGES[self.language],
            "mode": self.mode,
            "has_voice": self.embedding is not None,
        }

    async def send_json(self, message: dict):
        if not self.connected:
            return
        try:
            await self.ws.send_json(message)
        except Exception:
            self.connected = False

    async def send_bytes(self, data: bytes):
        if not self.connected:
            return
        try:
            await self.ws.send_bytes(data)
        except Exception:
            self.connected = False


@dataclass(eq=False)
class Room:
    id: str
    participants: dict[str, Participant] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_turn: int = 1
    empty_since: float | None = None

    def other(self, pid: str) -> Participant | None:
        return next((p for p in self.participants.values() if p.pid != pid), None)

    async def broadcast(self, message: dict):
        await asyncio.gather(*(p.send_json(message) for p in self.participants.values()))

    async def send_state(self):
        await self.broadcast({"type": "room", "participants": [p.public() for p in self.participants.values()]})


class RoomManager:
    def __init__(self, gnani: GnaniClient, translator: Translator, signer: VoiceSigner,
                 limiter: RateLimiter, default_voice: str):
        self.gnani = gnani
        self.translator = translator
        self.signer = signer
        self.limiter = limiter
        self.default_voice = default_voice
        self.rooms: dict[str, Room] = {}
        self._tasks: set[asyncio.Task] = set()

    def create(self) -> str:
        self._drop_stale_rooms()
        while True:
            room_id = "".join(secrets.choice(ROOM_ID_CHARS) for _ in range(ROOM_ID_LENGTH))
            if room_id not in self.rooms:
                self.rooms[room_id] = Room(room_id, empty_since=time.monotonic())
                return room_id

    def _drop_stale_rooms(self):
        now = time.monotonic()
        for room_id, room in list(self.rooms.items()):
            if not room.participants and room.empty_since and now - room.empty_since > EMPTY_ROOM_TTL:
                del self.rooms[room_id]

    async def handle(self, ws: WebSocket, room_id: str):
        await ws.accept()
        room = self.rooms.get(room_id.upper())
        if room is None:
            await ws.send_json({"type": "error", "fatal": True, "message": "This conversation link has expired. Start a new one."})
            await ws.close()
            return

        try:
            join = await ws.receive_json()
            participant = self._make_participant(ws, room, join)
            participant.ip = ws.client.host if ws.client else "unknown"
        except (ValueError, KeyError, TypeError) as e:
            await ws.send_json({"type": "error", "fatal": True, "message": str(e)})
            await ws.close()
            return
        except WebSocketDisconnect:
            return

        # A reconnect from the same tab replaces its stale connection
        stale = [
            p for p in room.participants.values()
            if p.user_id == participant.user_id and p.client_id == participant.client_id
        ]
        for old in stale:
            old.connected = False
            del room.participants[old.pid]
        if len(room.participants) >= MAX_PARTICIPANTS:
            await ws.send_json({"type": "error", "fatal": True, "message": "This conversation already has two people."})
            await ws.close()
            return

        room.participants[participant.pid] = participant
        room.empty_since = None
        await participant.send_json({"type": "joined", "you": participant.pid, "mode": participant.mode})
        await room.send_state()

        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if participant.talk:
                        self._feed(participant, message["bytes"])
                    else:
                        if self.limiter.allow("turn", participant.ip):
                            self._start_turn(room, participant, audio=message["bytes"])
                        else:
                            await participant.send_json({"type": "error", "message": TOO_MANY_TURNS})
                elif message.get("text"):
                    await self._on_text(room, participant, message["text"])
        except WebSocketDisconnect:
            pass
        finally:
            participant.connected = False
            if participant.talk:
                participant.talk.stream.cancel()
                participant.talk = None
            if room.participants.get(participant.pid) is participant:
                del room.participants[participant.pid]
            if not room.participants:
                room.empty_since = time.monotonic()
            await room.send_state()

    def _make_participant(self, ws: WebSocket, room: Room, join: dict) -> Participant:
        if join.get("type") != "join":
            raise ValueError("First message must be a join")
        user_id = join.get("user_id", "")
        if not valid_user_id(user_id):
            raise ValueError("Invalid user id")
        client_id = str(join.get("client_id", ""))
        if not CLIENT_ID.match(client_id):
            raise ValueError("Invalid client id")
        name = str(join.get("name", "")).strip()[:MAX_NAME_LENGTH] or "Guest"
        language = join.get("language")
        if language not in LANGUAGES:
            raise ValueError("Pick a supported language")
        voice = join.get("voice") if join.get("voice") in STOCK_VOICES else self.default_voice
        # The browser keeps its own cloned voice; only voices we signed are accepted
        embedding = self.signer.open(join.get("cloned_voice"))
        mode = join.get("mode") if join.get("mode") in MODES else "my_voice"
        if mode == "my_voice" and embedding is None:
            mode = "expressive"
        return Participant(ws, secrets.token_hex(4), user_id, client_id, name, language, mode, voice, embedding)

    async def _on_text(self, room: Room, participant: Participant, text: str):
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return
        kind = message.get("type")
        if kind == "mode" and message.get("mode") in MODES:
            if message["mode"] == "my_voice" and participant.embedding is None:
                await participant.send_json({"type": "error", "message": "Set up your voice first to use My Voice mode."})
                return
            participant.mode = message["mode"]
            await room.send_state()
        elif kind == "talk_start":
            await self._talk_start(room, participant)
        elif kind == "talk_end":
            await self._talk_end(room, participant, cancel=False)
        elif kind == "talk_cancel":
            await self._talk_end(room, participant, cancel=True)

    async def _talk_start(self, room: Room, participant: Participant):
        if participant.talk:
            participant.talk.stream.cancel()
        if room.other(participant.pid) is None:
            participant.talk = None
            await participant.send_json({"type": "error", "message": "Wait for the other person to join first."})
            return
        if not self.limiter.allow("turn", participant.ip):
            participant.talk = None
            await participant.send_json({"type": "error", "message": TOO_MANY_TURNS})
            return
        # Connect now, while they speak: the connection takes 0.6-1.2 s
        try:
            participant.talk = Talk(self.gnani.stream_stt(participant.language))
        except DailyLimitReached as e:
            participant.talk = None
            await participant.send_json({"type": "error", "message": _friendly(e)})
            return
        await room.broadcast({"type": "speaking", "speaker": participant.pid, "speaker_name": participant.name, "on": True})

    def _feed(self, participant: Participant, pcm: bytes):
        talk = participant.talk
        if len(talk.pcm) + len(pcm) > MAX_TURN_BYTES:
            return
        talk.pcm.extend(pcm)
        talk.stream.send(pcm)

    async def _talk_end(self, room: Room, participant: Participant, cancel: bool):
        talk, participant.talk = participant.talk, None
        await room.broadcast({"type": "speaking", "speaker": participant.pid, "speaker_name": participant.name, "on": False})
        if talk is None:
            return
        if cancel or len(talk.pcm) < MIN_TALK_BYTES:
            talk.stream.cancel()
            return
        self._start_turn(room, participant, talk=talk, released_at=time.perf_counter())

    def _start_turn(self, room: Room, speaker: Participant, audio: bytes | None = None,
                    talk: Talk | None = None, released_at: float | None = None):
        task = asyncio.create_task(self._turn(room, speaker, audio, talk, released_at))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish_talk(self, speaker: Participant, talk: Talk) -> str:
        try:
            return await talk.stream.finish()
        except GnaniError as e:
            # Streaming failed (network, service): transcribe what we kept with REST instead
            log.warning("Streaming STT failed, using REST: %s", e)
            return await self.gnani.transcribe(wav_from_pcm(bytes(talk.pcm), STREAM_RATE), "turn.wav", speaker.language)

    async def _turn(self, room: Room, speaker: Participant, audio: bytes | None,
                    talk: Talk | None = None, released_at: float | None = None):
        if audio is not None and len(audio) > MAX_TURN_BYTES:
            await speaker.send_json({"type": "error", "message": "That recording is too long. Keep each turn under 55 seconds."})
            return
        if room.other(speaker.pid) is None:
            if talk:
                talk.stream.cancel()
            await speaker.send_json({"type": "error", "message": "Wait for the other person to join first."})
            return

        turn_id = room.next_turn
        room.next_turn += 1
        base = {"turn_id": turn_id, "speaker": speaker.pid}

        transcript = None
        if talk:
            await room.broadcast({**base, "type": "status", "state": "translating"})
            # Finishing the stream happens outside the lock, alongside any turn still playing
            try:
                transcript = await self._finish_talk(speaker, talk)
            except GnaniError as e:
                log.warning("Turn %s failed: %s", turn_id, e)
                await room.broadcast({**base, "type": "status", "state": "failed", "message": _friendly(e)})
                return
            if not transcript:
                await room.broadcast({**base, "type": "status", "state": "failed", "message": _friendly(PipelineError("heard no speech"))})
                return

        if room.turn_lock.locked():
            await room.broadcast({**base, "type": "status", "state": "queued"})

        async with room.turn_lock:
            # Re-check after waiting: the listener may have left, or changed language
            listener = room.other(speaker.pid)
            if listener is None:
                await room.broadcast({**base, "type": "status", "state": "failed", "message": "The other person left."})
                return
            await room.broadcast({**base, "type": "status", "state": "translating"})

            async def on_text(transcript: str, translation: Translation):
                await room.broadcast({
                    **base,
                    "type": "text",
                    "speaker_name": speaker.name,
                    "src": speaker.language,
                    "tgt": listener.language,
                    "mode": speaker.mode,
                    "transcript": transcript,
                    "translation": translation.text,
                    "emotion": translation.emotion,
                })
                await listener.send_json({
                    **base,
                    "type": "audio_start",
                    "speaker_name": speaker.name,
                    "mode": speaker.mode,
                    "sample_rate": AUDIO_CONFIG["sample_rate"],
                })

            try:
                result = await run_turn(
                    self.gnani,
                    self.translator,
                    src=speaker.language,
                    tgt=listener.language,
                    audio=audio,
                    transcript=transcript,
                    started_at=released_at,
                    mode=speaker.mode,
                    embedding=speaker.embedding,
                    stock_voice=speaker.voice,
                    on_text=on_text,
                    on_audio=listener.send_bytes,
                )
            except (GnaniError, TranslationError, PipelineError, ValueError) as e:
                log.warning("Turn %s failed: %s", turn_id, e)
                await room.broadcast({**base, "type": "status", "state": "failed", "message": _friendly(e)})
                await listener.send_json({"type": "audio_end", "turn_id": turn_id})
                return

            await listener.send_json({"type": "audio_end", "turn_id": turn_id})
            await room.broadcast({**base, "type": "status", "state": "done", "timings": result.timings})
            log.info("Turn %s %s->%s %s: %s", turn_id, speaker.language, listener.language, speaker.mode, result.timings)


def _friendly(error: Exception) -> str:
    if isinstance(error, DailyLimitReached):
        return LIMIT_MESSAGE
    text = str(error)
    if "heard no speech" in text:
        return "Didn't catch that. Hold the button and speak a little louder."
    if "429" in text:
        return "The voice service is busy. Try again in a moment."
    return "Something went wrong with that turn. Please try again."
