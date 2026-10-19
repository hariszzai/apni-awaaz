"""Cloned voices, voice previews, and the stock voice list.

A cloned voice (a Gnani speaker embedding) is kept in the user's own browser, not on the
server. The server signs each voice it creates with SECRET_KEY and only accepts voices
carrying a valid signature. So:
  - nothing is lost when a free host wipes its disk on restart,
  - no database or login is needed,
  - a client can't use a voice that didn't come through our consent flow.
Deleting the voice in the browser deletes it; the server only keeps cached preview clips,
which are removed along with it.
"""
import hashlib
import hmac
import json
import re
from pathlib import Path

from .gnani import parse_embedding

USER_ID = re.compile(r"^[a-f0-9]{32}$")

# Timbre v3.0 stock voices for Expressive mode; every one speaks all 10 languages
STOCK_VOICES = {
    "Shivani": "female", "Lakshmi": "female", "Kalpana": "female", "Chetana": "female",
    "Vasudha": "female", "Riyaa": "female", "Tarika": "female",
    "Avinash": "male", "Shubham": "male", "Prakash": "male", "Devansh": "male",
    "Yashraj": "male", "Harshad": "male",
}

# "Hello! This is how my voice sounds." (checked by translating each back to English)
PREVIEW_TEXT = {
    "hi-IN": "नमस्ते! मेरी आवाज़ कुछ ऐसी सुनाई देती है।",
    "en-IN": "Hello! This is how my voice sounds.",
    "ta-IN": "வணக்கம்! என் குரல் இப்படித்தான் ஒலிக்கும்.",
    "te-IN": "నమస్కారం! నా గొంతు ఇలా వినిపిస్తుంది.",
    "kn-IN": "ನಮಸ್ಕಾರ! ನನ್ನ ಧ್ವನಿ ಹೀಗೆ ಕೇಳಿಸುತ್ತದೆ.",
    "ml-IN": "നമസ്കാരം! എന്റെ ശബ്ദം ഇങ്ങനെയാണ് കേൾക്കുന്നത്.",
    "mr-IN": "नमस्कार! माझा आवाज असा ऐकू येतो.",
    "bn-IN": "নমস্কার! আমার কণ্ঠস্বর এরকম শোনায়।",
    "gu-IN": "નમસ્તે! મારો અવાજ આવો સંભળાય છે.",
    "pa-IN": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਮੇਰੀ ਆਵਾਜ਼ ਇਸ ਤਰ੍ਹਾਂ ਸੁਣਾਈ ਦਿੰਦੀ ਹੈ।",
}
# Expressive voices preview with an emotion tag, since emotion is what that mode is for
EXPRESSIVE_PREFIX = "<excited> "


def valid_user_id(user_id: str) -> bool:
    return bool(USER_ID.match(user_id or ""))


class VoiceSigner:
    def __init__(self, secret: bytes):
        self._secret = secret

    def _sign(self, embedding: dict) -> str:
        payload = json.dumps(embedding, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def seal(self, embedding: dict) -> dict:
        """The voice as handed to the browser: the embedding plus our signature."""
        return {**embedding, "sig": self._sign(embedding)}

    def open(self, voice) -> dict | None:
        """The embedding inside a voice from a browser, or None if we didn't sign it."""
        if not isinstance(voice, dict) or not isinstance(voice.get("sig"), str):
            return None
        try:
            embedding = parse_embedding(voice)
        except Exception:
            return None
        if not hmac.compare_digest(self._sign(embedding), voice["sig"]):
            return None
        return embedding


def voice_id(embedding: dict) -> str:
    """A short, stable name for a voice, used to file its preview clips."""
    return hashlib.sha256(embedding["embedding"].encode()).hexdigest()[:24]


class PreviewCache:
    """Generated preview clips on disk, so each one costs Gnani credits only once.

    Losing the folder (a host restart) is harmless: clips are just made again.
    """

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def path(self, kind: str, name: str, language: str) -> Path:
        return self.folder / f"{kind}_{name}_{language}.wav"

    def get(self, kind: str, name: str, language: str) -> bytes | None:
        path = self.path(kind, name, language)
        return path.read_bytes() if path.exists() else None

    def put(self, kind: str, name: str, language: str, audio: bytes):
        self.path(kind, name, language).write_bytes(audio)

    def forget_clone(self, name: str) -> int:
        removed = 0
        for path in self.folder.glob(f"clone_{name}_*.wav"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed


class LegacyVoices:
    """Voices saved on the server by the first version of the app (data/voices/<user id>.json).

    Each is handed to its browser once, signed, and then deleted from the server.
    """

    def __init__(self, folder: Path):
        self.folder = folder

    def take(self, user_id: str) -> dict | None:
        if not valid_user_id(user_id):
            return None
        path = self.folder / f"{user_id}.json"
        if not path.exists():
            return None
        embedding = parse_embedding(json.loads(path.read_text()))
        path.unlink()
        return embedding
