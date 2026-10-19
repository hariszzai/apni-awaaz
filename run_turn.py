"""Run one Apni Awaaz conversation turn on an audio file, from the command line.

Examples (from the project folder):
  python run_turn.py voice_test/outputs/cloned_hindi.wav --from hi-IN --to ta-IN
  python run_turn.py my_clip.wav --from hi-IN --to kn-IN --mode expressive

Saves the translated audio and a JSON log of the turn to outputs/turns/.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from backend.config import ROOT, load_settings
from backend.gnani import GnaniClient, GnaniError, parse_embedding
from backend.languages import LANGUAGES
from backend.pipeline import MODES, PipelineError, run_turn
from backend.translator import TranslationError, Translator

DEFAULT_EMBEDDING = ROOT / "voice_test" / "outputs" / "embedding.json"
OUT_DIR = ROOT / "outputs" / "turns"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("audio", type=Path, help="speaker's audio clip (WAV, MP3, OGG, FLAC, AAC, M4A; up to 60 s)")
    p.add_argument("--from", dest="src", required=True, choices=LANGUAGES, help="language the speaker uses")
    p.add_argument("--to", dest="tgt", required=True, choices=LANGUAGES, help="language the listener hears")
    p.add_argument("--mode", choices=MODES, default="my_voice")
    p.add_argument("--embedding", type=Path, default=DEFAULT_EMBEDDING, help="voice embedding JSON (My Voice mode)")
    p.add_argument("--voice", help="stock Timbre v3.0 voice for Expressive mode (default: STOCK_VOICE or Shubham)")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if not args.audio.exists():
        print(f"Audio file not found: {args.audio}")
        return 1
    try:
        settings = load_settings()
    except RuntimeError as e:
        print(e)
        return 1

    embedding = None
    if args.mode == "my_voice":
        if not args.embedding.exists():
            print(f"Voice embedding not found: {args.embedding}. Run voice_test/test_clone.py first.")
            return 1
        embedding = parse_embedding(json.loads(args.embedding.read_text()))

    print(f"Translating {args.src} -> {args.tgt} ({args.mode}, LLM: {settings.llm_provider}/{settings.llm_model})")
    async with GnaniClient(settings.gnani_api_key) as gnani, Translator(
        settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_reasoning_effort
    ) as translator:
        try:
            result = await run_turn(
                gnani,
                translator,
                src=args.src,
                tgt=args.tgt,
                audio=args.audio.read_bytes(),
                filename=args.audio.name,
                mode=args.mode,
                embedding=embedding,
                stock_voice=args.voice or settings.stock_voice,
            )
        except (GnaniError, TranslationError, PipelineError) as e:
            print(f"Turn failed: {e}")
            return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}_{args.src}-{args.tgt}_{args.mode}"
    wav_path = OUT_DIR / f"{stem}.wav"
    wav_path.write_bytes(result.audio)
    log = {k: v for k, v in vars(result).items() if k != "audio"}
    log["input"] = str(args.audio)
    (OUT_DIR / f"{stem}.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Heard:      {result.transcript}")
    print(f"Translated: {result.translation}")
    if result.emotion:
        print(f"Emotion:    {result.emotion}")
    t = result.timings
    print(f"Timings:    STT {t['stt']}s, translate {t['translate']}s, TTS {t['tts']}s, total {t['total']}s")
    print(f"Latency:    first audio after {t['first_audio']}s (target < 3s)")
    print(f"Audio:      {wav_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles need this for Indic scripts
    sys.exit(asyncio.run(main()))
