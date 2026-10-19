"""Feasibility test for Apni Awaaz: does Gnani voice cloning sound good across languages?

Steps it runs:
  1. Create a speaker embedding from my_voice.wav
  2. Speak the same line in Hindi, Tamil and Kannada in the cloned voice
  3. Try emotion tags on the cloned voice, and on a stock Timbre v3.0 voice for comparison
  4. Transcribe the cloned Hindi clip back with Prisma STT as a sanity check

Usage (PowerShell):
  $env:GNANI_API_KEY = "your-key"
  python test_clone.py
"""
import json
import os
import sys
from pathlib import Path

import requests

BASE = "https://api.vachana.ai"
HERE = Path(__file__).parent
REFERENCE = HERE / "my_voice.wav"
OUT = HERE / "outputs"

API_KEY = os.environ.get("GNANI_API_KEY")
if not API_KEY:
    sys.exit('Set your key first:  $env:GNANI_API_KEY = "your-key"')
if not REFERENCE.exists():
    sys.exit("my_voice.wav not found. Run record_voice.py first.")

HEADERS = {"X-API-Key-ID": API_KEY}
AUDIO_CONFIG = {
    "sample_rate": 44100,
    "num_channels": 1,
    "sample_width": 2,
    "encoding": "linear_pcm",
    "container": "wav",
}

LINES = {
    "hindi": "नमस्ते! यह मेरी अपनी आवाज़ है। क्या आप मुझे पहचान सकते हैं?",
    "tamil": "வணக்கம்! இது என் சொந்த குரல். நீங்கள் என்னை அடையாளம் காண முடிகிறதா?",
    "kannada": "ನಮಸ್ಕಾರ! ಇದು ನನ್ನ ಸ್ವಂತ ಧ್ವನಿ. ನೀವು ನನ್ನನ್ನು ಗುರುತಿಸಬಲ್ಲಿರಾ?",
}
EMOTION_LINE = "<excited> अरे वाह, यह तो कमाल हो गया! <laugh> क्या सच में यह मेरी आवाज़ है? <whispers> किसी को बताना मत।"


def save(name, response):
    if response.status_code != 200:
        print(f"  FAILED {name}: HTTP {response.status_code} {response.text[:300]}")
        return None
    path = OUT / f"{name}.wav"
    path.write_bytes(response.content)
    print(f"  saved {path.name}")
    return path


def make_embedding():
    print("1. Creating speaker embedding from my_voice.wav")
    with open(REFERENCE, "rb") as f:
        r = requests.post(
            f"{BASE}/api/v1/tts/voice-clone/embeddings",
            headers=HEADERS,
            files={"audio_file": ("my_voice.wav", f, "audio/wav")},
            timeout=60,
        )
    if r.status_code != 200:
        sys.exit(f"  Embedding failed: HTTP {r.status_code} {r.text[:300]}")
    data = r.json()
    (OUT / "embedding.json").write_text(json.dumps(data))
    # Live API nests the embedding under data.voice_clone_embedding (docs show it top-level)
    emb = data.get("data", {}).get("voice_clone_embedding", data)
    print(f"  ok, shape {emb.get('shape')}")
    return {k: emb[k] for k in ("embedding", "shape", "dtype")}


def cloned_tts(name, text, embedding):
    r = requests.post(
        f"{BASE}/api/v1/tts/inference",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={
            "text": text,
            "model": "vachana-vc-v1",
            "audio_config": AUDIO_CONFIG,
            "speaker_embedding": embedding,
        },
        timeout=120,
    )
    return save(name, r)


def stock_tts(name, text):
    r = requests.post(
        f"{BASE}/api/v1/tts/inference",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={
            "text": text,
            "voice": "Shubham",
            "model": "timbre-v3.0",
            "language": "hi-IN",
            "speed": 1.0,
            "audio_config": AUDIO_CONFIG,
        },
        timeout=120,
    )
    return save(name, r)


def transcribe(path):
    with open(path, "rb") as f:
        r = requests.post(
            f"{BASE}/stt/v3",
            headers=HEADERS,
            files={"audio_file": (path.name, f, "audio/wav")},
            data={"language_code": "hi-IN"},
            timeout=60,
        )
    if r.status_code != 200:
        print(f"  FAILED STT: HTTP {r.status_code} {r.text[:300]}")
        return
    print(f"  Prisma heard: {r.json().get('transcript')}")


def main():
    OUT.mkdir(exist_ok=True)
    embedding = make_embedding()

    print("2. Cloned voice in three languages")
    results = {lang: cloned_tts(f"cloned_{lang}", text, embedding) for lang, text in LINES.items()}

    print("3. Emotion tags")
    cloned_tts("cloned_emotion", EMOTION_LINE, embedding)
    stock_tts("stock_v3_emotion", EMOTION_LINE)

    if results["hindi"]:
        print("4. Prisma STT on the cloned Hindi clip")
        transcribe(results["hindi"])

    print(f"\nDone. Listen to the files in {OUT}")


if __name__ == "__main__":
    main()
