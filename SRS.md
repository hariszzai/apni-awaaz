# Apni Awaaz — Software Requirements Specification

**Version:** 1.0 (draft) · **Date:** 19 Oct 2026 · **Author:** Haris Nawaz
**Contest:** Great Indian AI Internship Challenge by Gnani AI · **Deadline:** 10 Nov 2026, 11:59 PM IST

---

## 1. The idea in one line

**Speak your language. Be heard in theirs, in your own voice.**

Apni Awaaz lets two people who don't share a language talk to each other. Each person speaks in their own language (Hinglish is fine). The other person hears it in *their* language, spoken in the **original speaker's own cloned voice**, not a robot voice.

## 2. Problem

India has 22 official languages and hundreds of dialects. Millions of everyday relationships cross a language line:

- **Inter-state families:** a Punjabi mother and a Tamil mother-in-law who smile at each other but can't really talk.
- **Migrant workers** and their landlords, employers or doctors in another state.
- **Students** who move to a new city for college.

Existing translators give you text, or a generic robotic voice. The conversation stops feeling like a conversation. You lose *who* is talking.

## 3. Target users

| User | Situation |
|---|---|
| Families across states | Parents and in-laws with no common language |
| Migrant workers | Talking to landlords, employers, doctors, police |
| Students and new residents | Daily life in a city whose language they don't speak |
| Small businesses | Shopkeepers and customers from other states |

## 4. Gnani AI models used

| Model | Used for |
|---|---|
| **Prisma v2.5** (speech-to-text) | Turning what each person says into text, including Hinglish and noisy phone-quality audio |
| **Voice cloning** (`vachana-vc-v1`) | Speaking the translation in the speaker's own voice |
| **Timbre v3.0** (text-to-speech) | "Expressive mode": stock voices that carry emotion (laughs, whispers, excitement) |

**Reasoning / translation layer:** Gnani's Evon model is not on the API and is too large to run on a laptop, so translation uses a separate LLM API behind a swappable interface (so Evon can be plugged in later). Current choice: **Groq, `openai/gpt-oss-120b`, reasoning effort "low"** (about 1 s per translation). Any OpenAI-compatible provider works by changing one env var (Gemini, or Evon self-hosted with SGLang/vLLM).

### Findings from the feasibility test (19 Oct)

- Cloned voice sounds like the user in **Hindi, Tamil and Kannada**.
- **Emotion tags don't work on cloned voices**, only on stock Timbre v3.0 voices. This is why there are two voice modes (section 5.3).
- Prisma transcribed a cloned Hindi clip almost perfectly.
- The live embeddings API wraps the result in `data.voice_clone_embedding` (the docs show it unwrapped).

### Findings from the pipeline tests (19 Oct)

- **One active request per API key.** A second simultaneous call gets HTTP 429. The backend queues all Gnani calls; the conversation room must handle two speakers on one key (section 7).
- **Cloned voices can't stream.** REST, SSE and WebSocket all deliver the audio only at the end, with a mostly fixed cost of about 2–3 s per call. Stock Timbre v3.0 does stream: first audio in about 0.25 s.
- **Cloned TTS breaks on multi-sentence text.** It became slow (10–18 s) and truncated, once returning only the first word. One sentence per call works reliably, so My Voice mode always synthesises sentence by sentence (long sentences split at commas, max ~100 characters).
- **Hinglish works with `hi-IN`.** Prisma transcribed a code-mixed clip correctly into Devanagari.
- Live WebSocket TTS sends base64 JSON messages, not the binary frames the docs describe.

### Findings from streaming STT (19 Oct)

- **Prisma realtime STT transcribes while the user is still talking**: it cuts speech into segments on silence and returns each one in ~130–180 ms.
- **There is no end-of-audio message.** Without trailing silence the last segment is never transcribed. The server sends 400 ms of silence (in real time; a burst was slower) with the silence threshold at 300 ms.
- **An open stream does not block other calls** on the same key, so one person talking never delays the other's audio.
- Connecting takes 0.6–1.2 s, so the stream opens when the button goes down and early audio is buffered.
- Closing the stream takes ~1 s (slow close handshake), so the transcript is used as soon as it's complete and the socket closes in the background.
- If streaming fails, the server transcribes the audio it kept with REST instead.

**Measured latency (button release → first audio, Hindi ↔ Tamil, streaming STT):**

| Mode | STT after release | First audio | Before streaming |
|---|---|---|---|
| Expressive (streamed stock voice) | 0.6–0.8 s | **1.7–2.2 s** ✅ | 2.2–2.9 s |
| My Voice (cloned, sentence by sentence) | 0.6–0.8 s | 4.5–5.2 s | 4.8–5.8 s |

My Voice is held back by cloned TTS itself: 2–3.8 s per sentence, even for one word, with no streaming. Possible next step: translate and synthesise finished segments *while the user is still talking*, so the first sentence is ready at release.

## 5. Functional requirements

### 5.1 Voice setup (onboarding)

- **FR-1:** The user records about 20 seconds of their voice in the app, with speaking prompts shown on screen, then **listens back** and chooses "Use this recording" or "Record again" before anything is cloned.
- **FR-2:** The app shows a consent screen first: "Your voice will be cloned only for your own conversations." The user must tick to continue.
- **FR-3:** The app creates a voice embedding through Gnani's cloning API. The embedding is **kept in the user's own browser**, signed by the server (HMAC with `SECRET_KEY`) so only voices made through this consent flow are accepted. Nothing is lost when a free host wipes its disk, and no login or database is needed.
- **FR-3a:** "Hear my cloned voice" plays a short sentence in the cloned voice, in the user's language (automatically right after cloning).
- **FR-3b:** "Re-record" replaces the voice; the old one stays until a new recording is used.
- **FR-3c:** The Expressive voice is chosen from a grid of the 13 Timbre v3.0 voices, each with a ▶ sample in the user's language.
- **FR-3d:** Preview clips are generated once and cached on the server, so replays cost no credits.
- **FR-4:** The user picks their language from the 10 Gnani supports (Hindi, English, Tamil, Telugu, Kannada, Malayalam, Marathi, Bengali, Gujarati, Punjabi).
- **FR-5:** The user can delete their voice at any time; this also deletes its cached preview clips on the server. One voice per person: cloning other people's voices is deliberately not supported.

### 5.2 Conversation room

- **FR-6:** User A creates a room and gets a short link or QR code. User B opens it on their own phone.
- **FR-7:** Each person talks with a **push-to-talk** button (hold to speak, release to send). Recording stops automatically at **55 seconds**, with a countdown in the last 10 s (Prisma REST rejects audio over 60 s).
- **FR-8:** Pipeline for every turn:
  1. Record audio in the browser
  2. Prisma STT → text in the speaker's language
  3. LLM → translation into the listener's language, keeping names, numbers and tone
  4. Cloned TTS → audio in the speaker's voice, in the listener's language
  5. Play it on the listener's phone automatically
- **FR-9:** Both screens show a live transcript: original text and translation side by side.
- **FR-10:** Hinglish and other code-mixed speech must be handled. The LLM translates the meaning, not word by word.

### 5.3 Voice modes

- **FR-11: My Voice mode (default):** the translation is spoken in the speaker's cloned voice. Neutral tone.
- **FR-12: Expressive mode:** the LLM detects emotion in what was said (happy, laughing, worried, whispering) and adds Timbre v3.0 emotion tags. The translation is spoken in a stock voice the user picks during setup.
- **FR-13:** The user can switch modes during a conversation.

### 5.4 Phone-quality mode (for the Noise Canceller award)

- **FR-14:** A "phone line" toggle downsamples audio to 8 kHz and can add background noise, to show the pipeline still works on telephonic audio.
- **FR-15 (stretch):** Real phone-call support through a telephony provider, so one side can join from a normal phone call.

### 5.5 After the conversation

- **FR-16:** At the end, both people get a short summary of the conversation in **their own language**, e.g. "You agreed the rent is ₹8,000, due on the 5th."
- **FR-17:** The transcript can be downloaded.

## 6. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Latency:** under 3 seconds from releasing the button until the translation **starts playing**. Expressive mode meets this (1.7–2.2 s with streaming STT). My Voice mode is at 4.5–5.2 s, limited by cloned TTS; the listener sees "is speaking…" and "Translating…" meanwhile |
| NFR-2 | **Works on phones:** mobile browser, no app install |
| NFR-3 | **Privacy:** synthetic or self-made data only; no real phone numbers, Aadhaar, PAN or recorded calls of other people (contest rule) |
| NFR-4 | **Honest AI:** every cloned-voice playback starts with a short tone and an "AI voice" label on screen |
| NFR-5 | **Security:** the API key stays on the server only, never in the browser |
| NFR-6 | **Credits:** stay within the 5,000 programme credits. Per-visitor hourly limits (6 clones, 60 previews, 30 conversations, 150 turns) plus a daily cap on all Gnani calls (`GNANI_DAILY_LIMIT`, default 1500) |

## 7. System architecture

```
 Phone A (browser)                    Server (Python, FastAPI)                 Phone B (browser)
 ─────────────────                    ────────────────────────                 ─────────────────
 hold to talk ──audio──▶  WebSocket room
                          │
                          ├─▶ Prisma STT ──────────▶ text (A's language)
                          ├─▶ LLM translate + tone ─▶ text (B's language)
                          └─▶ Cloned TTS / Timbre v3 ─▶ audio ──────────▶  auto-play
                                                                           transcript
 transcript ◀──────────── both texts ───────────────────────────────────▶  transcript
```

**Tech stack**

| Part | Choice | Why |
|---|---|---|
| Backend | Python + FastAPI + WebSockets | You already use Python; WebSockets connect the two phones |
| Frontend | Plain HTML/CSS/JavaScript (MediaRecorder API) | No build step; works on phone browsers |
| Speech | Gnani Prisma, cloning, Timbre | Contest requirement |
| Translation | Groq `openai/gpt-oss-120b` behind an OpenAI-compatible interface | Evon isn't on the API yet; swappable by env var |
| Storage | Cloned voices in each user's browser (signed); preview clips cached on the server's disk | No database or login; survives host restarts |
| Hosting | Hugging Face Spaces (Docker): `Dockerfile` + README settings, `--public` mode on `$PORT` | Free, HTTPS, WebSockets; see README for the steps |

**Two speakers, one key:** Gnani allows one active request per key, so the room processes turns one at a time: a turn sent while another is being translated waits in a queue, and the UI shows "waiting…". (Option to test: give each speaker one of the 3 free keys.)

## 8. Screens

1. **Landing:** a scrolling page over a live particle background (`static/particles.js`, Canvas 2D). The particles form a voice orb in the hero and change shape per section: two orbs talking (How it works), a waveform (Hear it), Indian script letters (Languages), a ring (Built on Gnani). They move away from the cursor or a finger, a click sends a shockwave, and the orb pulses with voice in the room. Sections: How it works (sticky phone mockup), Hear it yourself (the cloned-voice clips), Two ways to be heard, Languages, Built on Gnani AI, Consent, final call to action
2. **Consent and recording:** reading script, record button, playback
3. **Language picker**
4. **Home:** "Start a conversation" (shows QR/link) or "Join"
5. **Conversation room:** big push-to-talk button, live two-language transcript, mode toggle, phone-line toggle
6. **Summary:** conversation summary in your language, download transcript

## 9. Out of scope

- Gnani's Agent Builder (the contest excludes it)
- Group calls with more than two people
- Cloning anyone's voice except the user's own
- Native Android or iOS apps

## 10. Build plan

| Dates | Goal |
|---|---|
Engagement decides the shortlist, and it needs days to build, so the final demo goes out on **1–2 Nov**, not at the end. Polish after the post doesn't change the video, so it can wait.

| Dates | Goal |
|---|---|
| **19 Oct** ✅ | Feasibility test: cloning works in 3 languages |
| **19 Oct** ✅ | Backend pipeline: one turn from audio file → translated audio, both modes, latency tuning |
| **19 Oct** ✅ | Two-phone conversation room, push-to-talk, transcript, Expressive toggle, new UI |
| **19 Oct** ✅ | Phone testing over local HTTPS (works on two phones); streaming STT |
| **19 Oct** ✅ | Voice previews, re-record, voice picker; ready to host (Dockerfile, limits, README) |
| 24–26 Oct | Go live on Hugging Face Spaces; test from outside your Wi-Fi |
| 27–28 Oct | Phone-line mode, conversation summary |
| 29–31 Oct | Record the demo video (60-second cut + longer cut) |
| **1–2 Nov** | Post final demo on LinkedIn and X; community sharing |
| 3–9 Nov | Keep the post alive (reply to every comment), small fixes, written explanation |
| 9–10 Nov | Final submission (with buffer before the deadline) |

**Build-in-public posts** (to grow reach before the final demo):
- ~21 Oct: "My voice speaking Tamil and Kannada, languages I don't know" (use the test clips)
- ~25 Oct: first two-phone conversation working
- 1–2 Nov: final demo

## 11. Open questions

1. ~~Which LLM for translation?~~ **Closed:** Groq `openai/gpt-oss-120b`, reasoning effort low (section 4).
2. ~~Language code for Hinglish?~~ **Closed:** `hi-IN` handles code-mixed speech (tested on a synthetic clip; retest on real noisy speech).
3. **How many credits does one conversation turn cost?** Check the dashboard now that about 20 pipeline test calls have run.
4. **Hosting:** which free platform supports WebSockets and HTTPS reliably?
5. ~~Streaming?~~ **Closed:** push-to-talk audio streams to Prisma's WebSocket STT while the user speaks (section 4).
6. **Can each speaker use their own API key** to avoid the one-request-per-key queue? Do keys share one credit balance?

## 12. Demo story (for the video)

Two people who share no language: one speaks Hindi or Hinglish, the other Tamil or Kannada. They have a short, warm real conversation through Apni Awaaz. The moment that lands is the listener hearing the speaker's **own voice** in their language. The full video plan (hook, script, captions, posting) comes after the build is finished.
