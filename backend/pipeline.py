"""One conversation turn: speaker's audio -> Prisma STT -> LLM translation -> TTS audio."""
import asyncio
import io
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import soundfile as sf

from .gnani import STT_MAX_SECONDS, GnaniClient, pcm_from_wav, wav_from_pcm
from .languages import language_name
from .translator import Translation, Translator

MODES = ("my_voice", "expressive")

# My Voice mode: cloned TTS must get one sentence per call. Multi-sentence text comes back
# slow (10-18 s) and truncated, sometimes only the first word (tested 19 Oct). Splitting also
# lets sentence 1 play while the next ones are synthesised, one at a time (one active
# request per key).
SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+")
CLAUSE_END = re.compile(r"(?<=[,;])\s+")
MIN_SENTENCE_CHARS = 20
MAX_SENTENCE_CHARS = 100

AudioSink = Callable[[bytes], Awaitable[None]]
TextSink = Callable[[str, Translation], Awaitable[None]]


class PipelineError(RuntimeError):
    pass


@dataclass
class TurnResult:
    mode: str
    src: str
    tgt: str
    transcript: str
    translation: str
    emotion: str | None
    audio: bytes = field(repr=False)  # complete WAV
    timings: dict[str, float]


def audio_seconds(audio: bytes) -> float | None:
    """Duration of the clip, or None if soundfile can't read the format (Prisma may still accept it)."""
    try:
        return sf.info(io.BytesIO(audio)).duration
    except Exception:
        return None


def _merge_short(parts: list[str], joiner: str = " ") -> list[str]:
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) < MIN_SENTENCE_CHARS:
            merged[-1] += joiner + part
        elif part:
            merged.append(part)
    return merged


def split_sentences(text: str) -> list[str]:
    """Split into sentences (long ones at commas), merging fragments too short to sound natural."""
    chunks: list[str] = []
    for sentence in _merge_short(SENTENCE_END.split(text.strip())):
        if len(sentence) <= MAX_SENTENCE_CHARS:
            chunks.append(sentence)
            continue
        current = ""
        for clause in CLAUSE_END.split(sentence):
            if current and len(current) + len(clause) + 1 > MAX_SENTENCE_CHARS:
                chunks.append(current)
                current = clause
            else:
                current = f"{current} {clause}".strip()
        chunks.append(current)
    return chunks or [text]


async def run_turn(
    gnani: GnaniClient,
    translator: Translator,
    src: str,
    tgt: str,
    audio: bytes | None = None,
    filename: str = "turn.wav",
    transcript: str | None = None,
    started_at: float | None = None,
    mode: str = "my_voice",
    embedding: dict | None = None,
    stock_voice: str = "Shubham",
    on_text: TextSink | None = None,
    on_audio: AudioSink | None = None,
) -> TurnResult:
    """Run one turn, from an audio clip (Prisma REST) or a transcript already made by streaming STT.

    started_at (a time.perf_counter() value) is when the user released the button; timings
    are measured from it, so they show the wait the listener actually feels.
    on_text gets (transcript, translation) before speech synthesis starts, so the transcript
    can show while audio is still being made. on_audio receives raw PCM chunks
    (44.1 kHz, 16-bit mono) as soon as they're ready.
    """
    if mode not in MODES:
        raise PipelineError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "my_voice" and embedding is None:
        raise PipelineError("My Voice mode needs the speaker's voice embedding")
    if (audio is None) == (transcript is None):
        raise PipelineError("Pass either audio or a transcript")
    language_name(src), language_name(tgt)  # fail fast on bad codes

    timings = {}
    start = started_at or time.perf_counter()

    if transcript is None:
        seconds = audio_seconds(audio)
        if seconds is not None and seconds > STT_MAX_SECONDS:
            raise PipelineError(f"Clip is {seconds:.0f} s; Prisma REST accepts up to {STT_MAX_SECONDS} s")
        transcript = await gnani.transcribe(audio, filename, src)
    transcript = transcript.strip()
    timings["stt"] = time.perf_counter() - start
    if not transcript:
        raise PipelineError("Prisma heard no speech in the clip")

    t = time.perf_counter()
    translation = await translator.translate(transcript, src, tgt, expressive=mode == "expressive")
    timings["translate"] = time.perf_counter() - t
    if on_text:
        await on_text(transcript, translation)

    pcm = bytearray()

    async def emit(chunk: bytes):
        if "first_audio" not in timings:
            timings["first_audio"] = time.perf_counter() - start
        pcm.extend(chunk)
        if on_audio:
            await on_audio(chunk)

    t = time.perf_counter()
    if mode == "my_voice":
        async def synth(sentence: str) -> bytes:
            return pcm_from_wav(await gnani.cloned_tts(sentence, embedding))

        tasks = [asyncio.create_task(synth(s)) for s in split_sentences(translation.text)]
        try:
            for task in tasks:
                await emit(await task)
        finally:
            for task in tasks:
                task.cancel()
    else:
        async for chunk in gnani.stream_stock_tts(translation.text, stock_voice, tgt):
            await emit(chunk)
    timings["tts"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - start

    return TurnResult(
        mode=mode,
        src=src,
        tgt=tgt,
        transcript=transcript,
        translation=translation.text,
        emotion=translation.emotion,
        audio=wav_from_pcm(bytes(pcm)),
        timings={k: round(v, 2) for k, v in timings.items()},
    )
