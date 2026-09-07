# CallLab

CallLab is an autonomous AI patient simulator for testing conversational
healthcare-office agents over real phone calls.

It places a live Twilio call, streams bidirectional audio through Twilio Media
Streams, and speaks as a scenario-driven patient (Jane Doe) using **OpenAI
Realtime** by default. Gemini Live remains an optional fallback. Scenarios are
YAML files; the harness cycles them automatically or you can pick one by hand.
Transcripts and diagnostics land under `calls/` and stay gitignored.

## Why it exists

The challenge asked for scenario-driven phone testing, recordings/transcripts,
bug discovery, and iterative AI-assisted debugging. CallLab answers that with:

- Real telephone interaction (not mocked chat)
- Reusable patient scenarios instead of hardcoded scripts
- Local transcript + diagnostics per run
- Easy reruns of the same scenario
- Coverage for scheduling, reschedule, cancel, refill, hours/location/insurance,
  unclear requests, change-of-mind, corrections, interruption, multi-intent,
  human escalation, nurse/clinical escalation, and specific-provider preference

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env` with your own keys and numbers. Install
[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
so `run.py` can expose the local FastAPI server for Twilio Media Streams.

Never put real credentials in the README or commit `.env`.

## Run

Assessment call (auto-cycles scenarios `01 → … → 15 → 01`):

```powershell
python run.py
```

Cycle position lives in `.calllab_state.json` and advances only after Twilio
returns a Call SID. Manual overrides do not move the pointer:

```powershell
python run.py --scenario 1
python run.py --scenario 04_medication_refill
```

Self-test against your own phone (uses `SELF_TEST_NUMBER`; does not dial the
assessment line; does not advance the cycle):

```powershell
python run.py --self-test
python run.py --self-test --scenario 7
```

What a run does:

1. Starts the FastAPI voice server with the selected scenario
2. Opens a temporary Cloudflare tunnel
3. Places a Twilio call with a Media Stream into `/media-stream`
4. Streams audio to the voice provider as the scenario patient
5. Writes `transcript.txt` and `diagnostics.log` under `calls/<run-id>/`

## Validate without placing a call

There is no dial-out dry-run flag. You can still check that every scenario
loads and builds a patient prompt:

```powershell
python -c "import sys; sys.path.insert(0, 'src'); from scenarios import list_scenarios, load_scenario, build_patient_prompt; items = list_scenarios(); assert [i['number'] for i in items] == list(range(1, 16)); [build_patient_prompt(load_scenario(i['path'])) for i in items]; print(f'OK: {len(items)} scenarios')"
```

```powershell
python run.py --help
```

## Scenarios

| Id | Purpose |
| --- | --- |
| `01_basic_scheduling` | Book a new routine appointment |
| `02_reschedule_existing` | Move an existing appointment |
| `03_cancel_appointment` | Cancel an existing appointment |
| `04_medication_refill` | Request a routine medication refill |
| `05_office_hours` | Ask when the office is open |
| `06_location_question` | Ask which location / where the office is |
| `07_insurance_question` | Ask whether a named plan is accepted |
| `08_unclear_request` | Vague opening; office must clarify intent |
| `09_change_mind` | Change appointment preference once mid-flow |
| `10_conflicting_information` | Follow the office's corrected date/time |
| `11_interruption` | One brief natural barge-in, then recover |
| `12_multi_intent` | Schedule first, then ask about insurance |
| `13_request_human` | Ask to speak with a human representative |
| `14_nurse_escalation` | Non-emergency clinical concern → nurse |
| `15_specific_doctor` | Prefer a specific provider |

## Providers

Default is OpenAI Realtime (`AI_VOICE_PROVIDER=openai`):

```
AI_VOICE_PROVIDER=openai
OPENAI_API_KEY=your_openai_api_key
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_VOICE=sage
```

Optional Gemini Live fallback:

```
AI_VOICE_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key
```

OpenAI path uses CallLab-owned office turn detection, explicit
`input_audio_buffer.commit`, and PCMU/8000 both ways.

## Layout

| Path | Purpose |
| --- | --- |
| `run.py` | Starts server, tunnel, and Twilio call |
| `src/` | FastAPI media server, scenario loader, providers |
| `scenarios/` | YAML patient scenarios (01–15) |
| `calls/` | Local transcripts/diagnostics (gitignored) |

See `ARCHITECTURE.md` for the audio path and turn-taking design, and
`BUGS.md` for known issues found during live testing.
