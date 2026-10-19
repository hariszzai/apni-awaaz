---
title: Apni Awaaz
emoji: 🗣️
colorFrom: gray
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
short_description: Speak your language. Be heard in theirs, in your own voice.
---

# Apni Awaaz

**Speak your language. Be heard in theirs, in your own voice.**

Two people who share no language talk through their phones. Each speaks their own language (Hinglish is fine). The other hears it in *their* language, in the speaker's **own cloned voice**, or in an expressive voice that carries laughs, whispers and excitement.

Built for the **Great Indian AI Internship Challenge** on Gnani AI's speech models. #GnaniAI #GreatIndianAIInternshipChallenge

## How it works

| Step | Model |
|---|---|
| Push-to-talk audio streams while you speak | **Gnani Prisma v2.5**, realtime speech-to-text |
| The transcript is translated, keeping names, numbers and tone | Open-weight LLM via Groq (`openai/gpt-oss-120b`), swappable for Gnani Evon |
| The translation is spoken in your cloned voice | **Gnani voice cloning** (`vachana-vc-v1`) |
| …or in an expressive voice with emotion tags | **Gnani Timbre v3.0**, streamed |

Measured from releasing the button to hearing the translation: about **1.7–2.2 s** in Expressive mode and **4.5–5 s** in My Voice mode (cloned speech can't stream yet).

**Privacy:** nothing is cloned without ticking the consent box; a cloned voice is kept in the user's own browser (signed by the server so only voices made here are accepted); every translated voice is labelled "AI voice" while it plays; one tap deletes it.

## Run it on your computer

Needs Python 3.11+.

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `GNANI_API_KEY` and `GROQ_API_KEY`. Then:

```bash
python -m backend.server --https
```

- Laptop: `https://localhost:8443`
- Phones on the same Wi-Fi or hotspot: the `https://<laptop IP>:8443` address the server prints. The browser warns about the test certificate: tap **Advanced → Proceed**. Phones only allow the microphone on https pages.
- In VS Code: open the folder, pick the Python interpreter, and press **F5** (configurations are in `.vscode/launch.json`).

Without `--https` the server runs on `http://localhost:8000` for the laptop only.

## Put it online (Render, free)

The app needs a server that stays running (WebSockets, live rooms), so static hosts like Vercel or Netlify won't work. Render's free tier runs the `Dockerfile` and reads `render.yaml`.

1. Push this project to a GitHub repository (`.gitignore` keeps `.env`, recordings and certificates out).
2. On Render: **New → Blueprint**, pick the repository. Render reads `render.yaml`, asks for `GNANI_API_KEY` and `GROQ_API_KEY`, and generates `SECRET_KEY` itself (it signs saved voices, so it must never change).
3. Render builds and starts it at `https://apni-awaaz-xxxx.onrender.com`.

The free tier sleeps after 15 minutes without visitors and takes about a minute to wake: open the link yourself before sharing it or recording a demo.

**Other hosts:** anything that runs a Dockerfile works the same way: set the same secrets; the server listens on `$PORT` (default 7860). Hugging Face Spaces needs a PRO plan for Docker Spaces; with one, `python deploy_to_hf.py USER/SPACE` uploads just the needed files (the settings block at the top of this file configures the Space).

## Protecting the credits

A public link means anyone can use your 5,000 programme credits, so the server limits each visitor (IP address) per hour: 6 voice clones, 60 previews, 30 new conversations, 150 turns. On top of that, `GNANI_DAILY_LIMIT` caps all Gnani calls per day; after that, visitors see "The demo has reached today's limit". Previews are made once and cached. Watch real usage on the Gnani dashboard and adjust.

## Project layout

```
backend/
  server.py      web server: pages, voice + preview API, rooms; --https / --public modes
  rooms.py       two-person rooms over WebSocket, push-to-talk turns, queueing
  pipeline.py    one turn: speech-to-text → translation → speech
  gnani.py       Gnani API client (Prisma STT incl. streaming, Timbre, voice cloning)
  translator.py  translation over any OpenAI-compatible LLM API
  voices.py      signed voices, preview sentences and cache, stock voice list
  limits.py      per-visitor rate limits
  certs.py       self-signed certificate for phone testing
  config.py      settings from .env / environment
static/          the web app: page, styles, app logic, particle background
run_turn.py      run one translation turn on an audio file from the command line
voice_test/      the original voice cloning feasibility test (pip install -r requirements-dev.txt)
SRS.md           requirements, findings and build plan
```
