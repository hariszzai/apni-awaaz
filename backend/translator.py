"""Translation layer over any OpenAI-compatible chat API (Groq, Gemini, or self-hosted Evon)."""
import json
import re
from dataclasses import dataclass

import httpx

from .gnani import EMOTION_TAGS
from .languages import language_name

BASE_PROMPT = """You translate a spoken conversation between two people in India.
Translate what the speaker said from {src} into {tgt}.
- The input is a speech transcript and may be code-mixed (for example Hinglish). Translate the meaning naturally, the way a native {tgt} speaker would say it out loud, not word by word.
- Exclamations and fillers (arey, waah, yaar, achha, aiyo, ...) become the natural {tgt} equivalent a native speaker would say. Never spell them out phonetically in {tgt} script.
  Example, Hindi to Tamil: "अरे वाह" becomes "ஆஹா", not "அரே வா"; "यार" becomes "டா" or is dropped, not "யார்".
- Keep names, numbers, amounts and dates exactly.
- Write in the native script of {tgt}.
- Keep the speaker's tone: polite, casual, worried, happy."""

PLAIN_SUFFIX = "\nReply with only the translation, nothing else."

EXPRESSIVE_SUFFIX = """
- Also detect how the speaker feels. Insert emotion tags where that feeling shows, using only these tags: {tags}.
  Use at most two tags and none if the tone is neutral. A tag goes right BEFORE the words it colours, never at the end, e.g. "<laugh> That is so funny!"
Reply with JSON only: {{"emotion": "<one word>", "text": "<translation with tags>"}}"""

TAG_RE = re.compile(r"<\s*([a-z ]+?)\s*>", re.IGNORECASE)


class TranslationError(RuntimeError):
    pass


@dataclass
class Translation:
    text: str
    emotion: str | None = None


def clean_tags(text: str, keep: bool) -> str:
    """Drop tags Timbre doesn't know (or all tags when keep=False), and tidy the spacing."""
    def replace(m: re.Match) -> str:
        tag = m.group(1).lower()
        return f"<{tag}>" if keep and tag in EMOTION_TAGS else " "

    cleaned = re.sub(r"\s+", " ", TAG_RE.sub(replace, text)).strip()
    if not TAG_RE.sub("", cleaned).strip():
        raise TranslationError(f"Translation has no speakable text: {text!r}")
    # A tag at the very end colours nothing; move it to the front
    trailing = re.search(r"(?:\s*<[a-z ]+>)+$", cleaned)
    if trailing:
        cleaned = f"{trailing.group().strip()} {cleaned[: trailing.start()].strip()}"
    return cleaned


def _parse_json(content: str) -> dict:
    # Some models wrap JSON in ```json fences despite being asked not to
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise TranslationError(f"LLM did not return JSON: {content[:200]!r}") from None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise TranslationError(f"LLM JSON has no 'text' field: {content[:200]!r}")
    return data


class Translator:
    def __init__(self, base_url: str, api_key: str, model: str, reasoning_effort: str = "", timeout: float = 60):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def translate(self, text: str, src: str, tgt: str, expressive: bool = False) -> Translation:
        system = BASE_PROMPT.format(src=language_name(src), tgt=language_name(tgt))
        if expressive:
            system += EXPRESSIVE_SUFFIX.format(tags=", ".join(f"<{t}>" for t in EMOTION_TAGS))
        else:
            system += PLAIN_SUFFIX

        body = {
            "model": self.model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if expressive:
            body["response_format"] = {"type": "json_object"}

        r = await self._http.post("chat/completions", json=body)
        if r.status_code != 200:
            raise TranslationError(f"LLM request failed: HTTP {r.status_code} {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"] or ""

        if not expressive:
            # Cloned voices would read stray tags out loud
            return Translation(clean_tags(content, keep=False))
        data = _parse_json(content)
        return Translation(clean_tags(data["text"], keep=True), data.get("emotion"))
