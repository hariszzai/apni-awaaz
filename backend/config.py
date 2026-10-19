"""Settings from environment variables, or from a .env file in the project root."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Every provider below speaks the OpenAI chat-completions format, so swapping is one env var.
# provider: (base URL, env var holding the key, default model, default reasoning effort)
# Translation doesn't need deep reasoning, and every reasoning token adds latency.
LLM_PRESETS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b", "low"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash", "low"),
    # Gnani Evon served with SGLang/vLLM exposes the same API
    "evon": ("http://localhost:30000/v1", "EVON_API_KEY", "gnani-evon-v3.3", ""),
}


@dataclass(frozen=True)
class Settings:
    gnani_api_key: str
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_reasoning_effort: str  # "" = don't send the parameter
    stock_voice: str
    secret_key: bytes  # signs cloned voices; must stay the same across restarts
    data_dir: Path
    gnani_daily_limit: int  # 0 = no cap


def load_settings() -> Settings:
    gnani_key = os.environ.get("GNANI_API_KEY")
    if not gnani_key:
        raise RuntimeError("GNANI_API_KEY is not set. Add it to .env (see .env.example).")

    provider = os.environ.get("LLM_PROVIDER", "groq").lower()
    if provider not in LLM_PRESETS:
        raise RuntimeError(f"LLM_PROVIDER must be one of {', '.join(LLM_PRESETS)}, got {provider!r}")
    base_url, key_var, model, effort = LLM_PRESETS[provider]
    llm_key = os.environ.get(key_var, "")
    if not llm_key and provider != "evon":
        raise RuntimeError(f"{key_var} is not set. Add it to .env (see .env.example).")

    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        secret_key=_secret_key(data_dir),
        data_dir=data_dir,
        gnani_daily_limit=int(os.environ.get("GNANI_DAILY_LIMIT", "1500")),
        gnani_api_key=gnani_key,
        llm_provider=provider,
        llm_base_url=os.environ.get("LLM_BASE_URL", base_url),
        llm_api_key=llm_key,
        llm_model=os.environ.get("LLM_MODEL", model),
        llm_reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", effort),
        stock_voice=os.environ.get("STOCK_VOICE", "Shubham"),
    )


def _secret_key(data_dir: Path) -> bytes:
    """SECRET_KEY from the environment; locally, a random one kept in the data folder.

    If it changed, every cloned voice saved in people's browsers would stop being accepted,
    so on a host it must be set as a fixed secret.
    """
    if key := os.environ.get("SECRET_KEY"):
        return key.encode()
    path = data_dir / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip().encode()
