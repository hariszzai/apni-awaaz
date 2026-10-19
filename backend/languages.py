"""Languages supported by both Prisma STT and Timbre v3.0 / voice cloning."""

LANGUAGES = {
    "hi-IN": "Hindi",
    "en-IN": "English",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "pa-IN": "Punjabi",
}


def language_name(code: str) -> str:
    try:
        return LANGUAGES[code]
    except KeyError:
        raise ValueError(f"Unsupported language code {code!r}. Use one of: {', '.join(LANGUAGES)}") from None
