# Architecture

CallLab is a live phone harness: Twilio carries the call, CallLab bridges
media, and an AI voice provider speaks as a YAML-driven patient.

## Audio path

```
Twilio call
     ↕ PCMU / 8 kHz
Twilio Media Streams
     ↕ WebSocket
CallLab (FastAPI /media-stream)
     ↕
OpenAI Realtime  (default; Gemini Live optional fallback)
     ↕
Scenario-driven Jane Doe patient
```

## Trees

```
Call_Lab/
  run.py        orchestration: server + tunnel + Twilio call
  src/          media server, config, scenario prompt builder, providers
  scenarios/    versioned YAML scenarios (data only)
  calls/        per-run transcripts + diagnostics (gitignored)
```

- **`run.py`** starts the voice server, creates a temporary Cloudflare tunnel,
  places the Twilio call, waits for completion, then shuts processes down.
- **`src/`** owns scenario loading, the media-stream bridge, provider sessions,
  and writing results under `calls/`.
- **`scenarios/`** owns identity, goals, behavior, and rules. Treat as data.
- **`calls/`** stays local so transcripts, SIDs, and PII never enter git.

## Runtime flow

1. `python run.py` or `python run.py --self-test`
2. FastAPI listens on port 8000 (`src/server.py`)
3. `cloudflared` exposes a temporary public HTTPS URL for Twilio
4. Twilio dials `PGAI_TEST_NUMBER` or `SELF_TEST_NUMBER`
5. Twilio Media Stream connects to `/media-stream`
6. Server routes audio to OpenAI Realtime (or Gemini Live) with a scenario-built
   patient prompt
7. Run folder `calls/<timestamp>-<scenario-id>/` gets `transcript.txt` and
   `diagnostics.log`

## Engineering problems this stack had to solve

### Realtime duplex audio

Twilio Media Streams deliver μ-law 8 kHz. OpenAI Realtime uses the same PCMU
path end-to-end. The Gemini fallback resamples between Twilio μ-law and Gemini
PCM rates inside `src/server.py`.

### Turn boundaries (CallLab-owned)

The OpenAI path disables provider auto turn detection (`turn_detection=null`)
and lets CallLab decide when the office finished speaking: silence threshold,
post-speech grace, and a minimum office-speech floor before treating a turn as
complete. That boundary triggers an explicit `input_audio_buffer.commit` so the
model responds to a finished office turn, not a half-heard fragment.

### Stale turns and response lifecycle

Responses are tracked by id through requested → created → active audio.
Watchdogs and response-id gating stop orphan or late audio from a previous
office turn from playing after the conversation has moved on.

### Twilio playback marks

Outbound patient audio is paced and closed with Twilio playback marks so CallLab
knows when the phone finished playing Jane’s turn before it treats the next
office audio as a fresh turn.

### Not speaking over the office

Inbound office audio is gated while Jane is speaking; deferred office audio can
be replayed afterward (with a byte/chunk cap — see `BUGS.md`). Persona rules
also tell Jane to listen, answer only what was asked, and stay silent after
status/progress filler when no reply is needed.

### Scenario progressive disclosure

`src/scenarios.py` builds a patient prompt from YAML. Scenario facts are
knowledge, not a script: Jane answers what the office asks, does not dump the
whole scenario up front, and never invents availability, transfers, or
confirmations the office did not state.

## Scenario cycling

Numbered YAML files under `scenarios/` are discovered automatically
(`01_…` through `15_…`). Automatic mode walks them in order and wraps
`15 → 01`. `.calllab_state.json` stores the next number and advances only after
a Twilio Call SID is obtained. `--scenario` and `--self-test` never advance the
production pointer.

## Boundaries

1. Scenario files must not import app internals from `src/`.
2. `src/` may read `scenarios/` and write `calls/`. It must not rewrite
   scenario files during a run.
3. Secrets live in `.env`. Code reads them through environment variables only.
4. Destination numbers are mode-specific: `run.py` requires `PGAI_TEST_NUMBER`
   for assessment mode and `SELF_TEST_NUMBER` for `--self-test`.

## Modules

- `src/config.py` — load `.env`; require the active voice-provider key + Twilio
  account settings; leave destination numbers to `run.py`
- `src/scenarios.py` — discover/load YAML; build the patient system prompt
- `src/providers/openai_realtime.py` — OpenAI Realtime media session (default)
- `src/server.py` — FastAPI WebSocket media bridge; dispatches openai | gemini
- `run.py` — process orchestration for a single call

## Config

Copy `.env.example` to `.env` and set the variables documented there. Do not
commit real keys, tokens, or phone numbers.
