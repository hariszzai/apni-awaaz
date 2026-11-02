"""Two-person conversation rooms over WebSocket.

Protocol (one WebSocket per person, /ws/{room_id}):

  client -> server
    {"type": "join", "user_id", "client_id", "name", "language", "mode",
     "voice" (Expressive voice name), "cloned_voice" (signed, kept by the browser),
     "rejoin" (optional: this tab was in the room, so recreate it if the server restarted),
     "solo" (optional: {"name", "language"} of a guest sharing this phone)}            first message
    {"type": "mode", "mode": "my_voice" | "expressive"}
    {"type": "guest_language", "language"}     one-phone rooms: the guest's language changed
    {"type": "talk_start", "as": "guest"?}     button pressed ("as": "guest" on one phone)
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
    {"type": "error", "message", "fatal"?}

One-phone rooms: two people share one phone, each with their own button. The guest is a
second participant without a socket of its own: everything meant for it goes to the host's.
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

from .gnani import (
    AUDIO_CONFIG,
    STREAM_RATE,
    STT_MAX_SECONDS,
    DailyLimitReached,
    GnaniClient,
    GnaniError,
    SttStream,
    wav_from_pcm,
)
from .languages import LANGUAGES
from .pipeline import MODES, PipelineError, run_turn
from .translator import Translation, TranslationError, Translator
from .limits import RateLimiter
from .voices import STOCK_VOICES, VoiceSigner, valid_user_id

log = logging.getLogger("apni_awaaz.rooms")

CLIENT_ID = re.compile(r"^[a-f0-9]{16,32}$")
ROOM_ID_CHARS = string.ascii_uppercase + string.digits
ROOM_ID_LENGTH = 6
ROOM_ID = re.compile(rf"^[A-Z0-9]{{{ROOM_ID_LENGTH}}}$")
MAX_PARTICIPANTS = 2
MAX_TURN_BYTES = 5 * 1024 * 1024  # a whole-turn WAV upload; Prisma REST then checks the length
FALLBACK_BYTES = STT_MAX_SECONDS * STREAM_RATE * 2  # streamed audio kept for the REST fallback
MIN_TALK_BYTES = int(0.3 * STREAM_RATE) * 2  # shorter presses are accidental taps
TOO_MANY_TURNS = "You're talking a lot! Please wait a few minutes before the next turn."
LIMIT_MESSAGE = "The demo has reached today's limit. Please try again tomorrow."
EMPTY_ROOM_TTL = 30 * 60  # keep empty rooms for reconnects (phones drop sockets on screen lock)
MAX_NAME_LENGTH = 30


@dataclass(eq=False)
class Talk:
    """A push-to-talk press in progress: audio goes to Prisma live, and is kept for a REST fallback."""
    stream: SttStream
    speaker: "Participant"  # on one phone, the guest talks through the host's socket
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
    relay: "Participant | None" = None  # a one-phone guest: messages go to the host's socket

    def public(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "language": self.language,
            "language_name": LANGUAGES[self.language],
            "mode": self.mode,
            "has_voice": self.embedding is not None,
            "guest": self.relay is not None,
        }

    async def send_json(self, message: dict):
        if self.relay:
            await self.relay.send_json(message)
            return
        if not self.connected:
            return
        try:
            await self.ws.send_json(message)
        except Exception:
            self.connected = False

    async def send_bytes(self, data: bytes):
        if self.relay:
            await self.relay.send_bytes(data)
            return
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
    solo: bool = False  # one phone shared by two people

    def other(self, pid: str) -> Participant | None:
        return next((p for p in self.participants.values() if p.pid != pid), None)

    def remove(self, participant: Participant):
        """Take someone out, with the guest who shares their phone."""
        for pid, p in list(self.participants.items()):
            if p is participant or p.relay is participant:
                del self.participants[pid]

    async def broadcast(self, message: dict):
        # A guest's messages would only reach the host's socket a second time
        await asyncio.gather(*(p.send_json(message) for p in self.participants.values() if not p.relay))

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

    def create(self, solo: bool = False) -> str:
        self._drop_stale_rooms()
        while True:
            room_id = "".join(secrets.choice(ROOM_ID_CHARS) for _ in range(ROOM_ID_LENGTH))
            if room_id not in self.rooms:
                self.rooms[room_id] = Room(room_id, empty_since=time.monotonic(), solo=solo)
                return room_id

    def _drop_stale_rooms(self):
        now = time.monotonic()
        for room_id, room in list(self.rooms.items()):
            if not room.participants and room.empty_since and now - room.empty_since > EMPTY_ROOM_TTL:
                del self.rooms[room_id]

    async def handle(self, ws: WebSocket, room_id: str):
        await ws.accept()
        room_id = room_id.upper()

        async def refuse(message: str):
            await ws.send_json({"type": "error", "fatal": True, "message": message})
            await ws.close()

        try:
            join = await ws.receive_json()
            participant = self._make_participant(ws, join)
            participant.ip = ws.client.host if ws.client else "unknown"
            solo = join.get("solo")
            guest = self._make_guest(participant, solo) if solo else None
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            await refuse(str(e))
            return
        except WebSocketDisconnect:
            return

        room = self.rooms.get(room_id)
        if room is None and join.get("rejoin") and ROOM_ID.match(room_id) and self.limiter.allow("room", participant.ip):
            # This tab was in the room before the server restarted (an update, or the free host
            # waking up): bring the room back so a conversation survives it
            room = self.rooms[room_id] = Room(room_id, solo=guest is not None)
        if room is None:
            await refuse("This conversation link has expired. Start a new one.")
            return

        # A reconnect from the same tab replaces its stale connection
        stale = [
            p for p in room.participants.values()
            if not p.relay and p.user_id == participant.user_id and p.client_id == participant.client_id
        ]
        for old in stale:
            old.connected = False
            room.remove(old)
        if room.solo != (guest is not None):
            await refuse("This conversation is on another phone." if room.solo else "Open this code on a second phone to join.")
            return
        if room.solo and room.participants:
            await refuse("This conversation is on another phone.")
            return
        if len(room.participants) >= MAX_PARTICIPANTS:
            await refuse("This conversation already has two people.")
            return

        room.participants[participant.pid] = participant
        if guest:
            room.participants[guest.pid] = guest
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
                room.remove(participant)
            if not room.participants:
                room.empty_since = time.monotonic()
            await room.send_state()

    def _make_participant(self, ws: WebSocket, join: dict) -> Participant:
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

    def _make_guest(self, host: Participant, solo: dict) -> Participant:
        """The person sharing the host's phone. Nobody cloned their voice, so they speak Expressive."""
        language = solo.get("language")
        if language not in LANGUAGES:
            raise ValueError("Pick the other person's language")
        name = str(solo.get("name", "")).strip()[:MAX_NAME_LENGTH] or "Guest"
        # A different voice from the host's, so the two sides sound like two people
        voice = next((v for v in STOCK_VOICES if v != host.voice), host.voice)
        guest = Participant(host.ws, "g" + secrets.token_hex(4), host.user_id, host.client_id,
                            name, language, "expressive", voice, None, relay=host)
        guest.ip = host.ip
        return guest

    def _guest_of(self, room: Room, host: Participant) -> Participant | None:
        return next((p for p in room.participants.values() if p.relay is host), None)

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
        elif kind == "guest_language" and message.get("language") in LANGUAGES:
            if guest := self._guest_of(room, participant):
                guest.language = message["language"]
                await room.send_state()
        elif kind == "talk_start":
            speaker = self._guest_of(room, participant) if message.get("as") == "guest" else participant
            if speaker:
                await self._talk_start(room, participant, speaker)
        elif kind == "talk_end":
            await self._talk_end(room, participant, cancel=False)
        elif kind == "talk_cancel":
            await self._talk_end(room, participant, cancel=True)

    async def _talk_start(self, room: Room, participant: Participant, speaker: Participant):
        if participant.talk:
            participant.talk.stream.cancel()
        if room.other(speaker.pid) is None:
            participant.talk = None
            await participant.send_json({"type": "error", "message": "Wait for the other person to join first."})
            return
        if not self.limiter.allow("turn", participant.ip):
            participant.talk = None
            await participant.send_json({"type": "error", "message": TOO_MANY_TURNS})
            return
        # Connect now, while they speak: the connection takes 0.6-1.2 s
        try:
            participant.talk = Talk(self.gnani.stream_stt(speaker.language), speaker)
        except DailyLimitReached as e:
            participant.talk = None
            await participant.send_json({"type": "error", "message": _friendly(e)})
            return
        await room.broadcast({"type": "speaking", "speaker": speaker.pid, "speaker_name": speaker.name, "on": True})

    def _feed(self, participant: Participant, pcm: bytes):
        talk = participant.talk
        talk.stream.send(pcm)
        if len(talk.pcm) + len(pcm) <= FALLBACK_BYTES:  # REST takes no more than this
            talk.pcm.extend(pcm)

    async def _talk_end(self, room: Room, participant: Participant, cancel: bool):
        talk, participant.talk = participant.talk, None
        speaker = talk.speaker if talk else participant
        await room.broadcast({"type": "speaking", "speaker": speaker.pid, "speaker_name": speaker.name, "on": False})
        if talk is None:
            return
        if cancel or len(talk.pcm) < MIN_TALK_BYTES:
            talk.stream.cancel()
            return
        self._start_turn(room, speaker, talk=talk, released_at=time.perf_counter())

    def _start_turn(self, room: Room, speaker: Participant, audio: bytes | None = None,
                    talk: Talk | None = None, released_at: float | None = None):
        task = asyncio.create_task(self._turn(room, speaker, audio, talk, released_at))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish_talk(self, speaker: Participant, talk: Talk) -> str:
        try:
            transcript = await talk.stream.finish()
            if transcript:
                return transcript
            # Now and then the stream never sends its last segment back (seen on 2 Nov): the
            # audio is still here, so don't tell the speaker we heard nothing
            log.warning("Streaming STT returned nothing, using REST")
        except GnaniError as e:
            # Streaming failed (network, service): transcribe what we kept with REST instead
            log.warning("Streaming STT failed, using REST: %s", e)
        return await self.gnani.transcribe(wav_from_pcm(bytes(talk.pcm), STREAM_RATE), "turn.wav", speaker.language)

    async def _turn(self, room: Room, speaker: Participant, audio: bytes | None,
                    talk: Talk | None = None, released_at: float | None = None):
        if audio is not None and len(audio) > MAX_TURN_BYTES:
            await speaker.send_json({"type": "error", "message": f"That recording is too long. Keep each turn under {STT_MAX_SECONDS} seconds."})
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
