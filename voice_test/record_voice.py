"""Record a reference clip of your voice for Gnani voice cloning.

Usage:  python record_voice.py            (records 20 seconds)
        python record_voice.py 25         (records 25 seconds)
"""
import sys
import wave
from pathlib import Path

import sounddevice as sd

SAMPLE_RATE = 16000
OUT_FILE = Path(__file__).parent / "my_voice.wav"

seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 20

print(f"Recording {seconds} seconds. Read the script in the instructions out loud, naturally.")
input("Press Enter to start...")
audio = sd.rec(seconds * SAMPLE_RATE, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
sd.wait()

with wave.open(str(OUT_FILE), "wb") as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(SAMPLE_RATE)
    f.writeframes(audio.tobytes())

print(f"Saved {OUT_FILE}. Play it back once to check it sounds clear.")
