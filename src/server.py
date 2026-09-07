import asyncio
import audioop
import base64
import json
import os
import re
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
import uvicorn

from config import AI_VOICE_PROVIDER, GEMINI_API_KEY
from scenarios import load_scenario, build_patient_prompt


app = FastAPI()

MODEL = "gemini-3.1-flash-live-preview"
# Selected by run.py via CALL_LAB_SCENARIO_PATH; default keeps prior behavior.
SCENARIO_PATH = os.environ.get(
    "CALL_LAB_SCENARIO_PATH",
    "scenarios/01_basic_scheduling.yaml",
)

# Client-side voice activity detection.
# This replaces Gemini's automatic VAD entirely.
SPEECH_RMS_THRESHOLD = 350
NORMAL_END_SILENCE_SECONDS = 0.90

# PGAI's opening contains natural pauses that are longer than a normal
# conversational pause. Do not close the first office turn before this point.
INITIAL_GREETING_MIN_SECONDS = 12.0
INITIAL_GREETING_END_SILENCE_SECONDS = 1.20

# Keep a little audio before detected speech so first syllables are not clipped.
PREBUFFER_SECONDS = 0.20
TWILIO_CHUNKS_PER_SECOND = 50
PREBUFFER_CHUNKS = int(PREBUFFER_SECONDS * TWILIO_CHUNKS_PER_SECOND)

# While the patient's audio is still physically playing through Twilio,
# office audio is held locally instead of being sent to Gemini. This prevents
# incoming PGAI speech from cancelling Gemini's active patient response.
DEFERRED_AUDIO_MAX_SECONDS = 12
DEFERRED_AUDIO_MAX_CHUNKS = (
    DEFERRED_AUDIO_MAX_SECONDS * TWILIO_CHUNKS_PER_SECOND
)

# Outbound aggregation only (no timer / no pacer / no silence).
# μ-law @ 8 kHz = 8000 bytes/sec.
STARTUP_BUFFER_BYTES = 4000  # ~500 ms before first Twilio send of a turn
SEND_BLOCK_BYTES = 1600  # ~200 ms real-audio blocks after startup


@app.get("/")
async def health():
    return {"status": "CallLab online"}


class StreamingAudioConverter:
    def __init__(self):
        self.twilio_to_gemini_state = None
        self.gemini_to_twilio_state = None

    def twilio_to_gemini(self, mulaw_8k: bytes) -> bytes:
        pcm_8k = audioop.ulaw2lin(mulaw_8k, 2)
        pcm_16k, self.twilio_to_gemini_state = audioop.ratecv(
            pcm_8k,
            2,
            1,
            8000,
            16000,
            self.twilio_to_gemini_state,
        )
        return pcm_16k

    def gemini_to_twilio(self, pcm_24k: bytes) -> bytes:
        pcm_8k, self.gemini_to_twilio_state = audioop.ratecv(
            pcm_24k,
            2,
            1,
            24000,
            8000,
            self.gemini_to_twilio_state,
        )
        return audioop.lin2ulaw(pcm_8k, 2)


def create_call_folder(scenario: dict) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path("calls") / f"{timestamp}-{scenario['id']}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def clean_fragments(fragments: list[str]) -> str:
    text = " ".join(
        fragment.strip()
        for fragment in fragments
        if fragment.strip()
    )
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def append_transcript(
    path: Path,
    speaker: str,
    fragments: list[str],
):
    text = clean_fragments(fragments)
    if not text:
        return

    timestamp = datetime.now().strftime("%H:%M:%S")
    with path.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {speaker}: {text}\n")

    print(f"{speaker}: {text}", flush=True)


def write_diagnostic(path: Path, message: str):
    timestamp = datetime.now().isoformat(timespec="milliseconds")
    line = f"[{timestamp}] {message}"

    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")

    print(f"[DIAG] {message}", flush=True)


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    scenario = load_scenario(SCENARIO_PATH)
    patient_prompt = build_patient_prompt(scenario)

    patient_prompt += """

VOICE AND DELIVERY

Speak with a kind, warm, gentle demeanor.
Sound calm and natural.
Keep responses concise and conversational.
Do not sound robotic, theatrical, or overly energetic.

PHONE TURN-TAKING

You are the patient.
Do not speak over the medical office.
When the office finishes, answer promptly.
If the office asks a direct question, answer it first before adding details.
"""

    call_folder = create_call_folder(scenario)
    transcript_path = call_folder / "transcript.txt"
    diagnostics_path = call_folder / "diagnostics.log"

    transcript_path.write_text(
        (
            f"Scenario: {scenario['name']}\n"
            f"Scenario ID: {scenario['id']}\n"
            f"Patient: {scenario['patient']['name']}\n"
            f"Provider: {AI_VOICE_PROVIDER}\n"
            f"Started: {datetime.now().isoformat()}\n\n"
        ),
        encoding="utf-8",
    )
    diagnostics_path.write_text("", encoding="utf-8")

    print(f"Scenario loaded: {scenario['name']}")
    print(f"Patient: {scenario['patient']['name']}")
    print(f"Provider: {AI_VOICE_PROVIDER}")
    print(f"Transcript: {transcript_path}")
    print(f"Diagnostics: {diagnostics_path}")

    write_diagnostic(
        diagnostics_path,
        f"Twilio WebSocket connected. Provider={AI_VOICE_PROVIDER}",
    )

    if AI_VOICE_PROVIDER == "openai":
        from providers.openai_realtime import run_openai_media_stream

        await run_openai_media_stream(
            websocket=websocket,
            scenario=scenario,
            patient_prompt=patient_prompt,
            transcript_path=transcript_path,
            diagnostics_path=diagnostics_path,
        )
        return

    # --- Gemini Live fallback path (unchanged protocol) ---
    client = genai.Client(api_key=GEMINI_API_KEY)

    # IMPORTANT:
    # Automatic VAD is OFF. CallLab explicitly tells Gemini when the office
    # starts and stops speaking. This prevents server-side VAD from cancelling
    # the patient's generation because PGAI talks over it.
    gemini_config = {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "realtime_input_config": {
            "automatic_activity_detection": {
                "disabled": True,
            }
        },
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": "Zephyr",
                }
            }
        },
        "system_instruction": patient_prompt,
    }

    stream_sid = None
    stream_active = asyncio.Event()

    converter = StreamingAudioConverter()

    office_buffer = []
    patient_buffer = []

    # Manual office turn state.
    activity_open = False
    first_office_turn_complete = False
    office_speaking = False
    office_speech_seen = False
    office_speech_started_at = None
    office_last_voice_at = None
    office_last_turn_end_at = None

    # Twilio playback state.
    patient_generation_active = False
    patient_playback_active = False
    patient_output_started_at = None
    patient_turn_number = 0
    pending_playback_mark = None

    # Gemini→Twilio aggregation buffer for the current patient turn.
    # Driven only by Gemini chunk arrivals and turn_complete (no clock).
    outbound_buffer = bytearray()
    outbound_startup_ready = False
    outbound_gemini_bytes = 0
    outbound_twilio_bytes = 0
    outbound_last_gemini_chunk_at = None
    outbound_max_gemini_gap_ms = 0.0

    # Audio waiting while patient is still playing through Twilio.
    deferred_audio = deque(maxlen=DEFERRED_AUDIO_MAX_CHUNKS)

    # Audio immediately preceding detected speech.
    prebuffer = deque(maxlen=PREBUFFER_CHUNKS)

    twilio_media_chunks = 0
    gemini_audio_chunks = 0

    session_start = asyncio.get_running_loop().time()

    def elapsed() -> float:
        return asyncio.get_running_loop().time() - session_start

    def reset_outbound_turn_state():
        nonlocal outbound_buffer
        nonlocal outbound_startup_ready
        nonlocal outbound_gemini_bytes
        nonlocal outbound_twilio_bytes
        nonlocal outbound_last_gemini_chunk_at
        nonlocal outbound_max_gemini_gap_ms

        outbound_buffer = bytearray()
        outbound_startup_ready = False
        outbound_gemini_bytes = 0
        outbound_twilio_bytes = 0
        outbound_last_gemini_chunk_at = None
        outbound_max_gemini_gap_ms = 0.0

    async def flush_remaining():
        if office_buffer:
            append_transcript(
                transcript_path,
                "Office",
                office_buffer,
            )
            office_buffer.clear()

        if patient_buffer:
            append_transcript(
                transcript_path,
                "Patient",
                patient_buffer,
            )
            patient_buffer.clear()

    try:
        write_diagnostic(
            diagnostics_path,
            f"Opening Gemini Live session with {MODEL}.",
        )

        async with client.aio.live.connect(
            model=MODEL,
            config=gemini_config,
        ) as gemini_session:

            write_diagnostic(
                diagnostics_path,
                "Gemini Live session connected with MANUAL VAD.",
            )

            async def send_activity_start():
                nonlocal activity_open

                if activity_open:
                    return

                await gemini_session.send_realtime_input(
                    activity_start=types.ActivityStart()
                )
                activity_open = True
                write_diagnostic(
                    diagnostics_path,
                    f"Gemini office activity START at {elapsed():.1f}s.",
                )

            async def send_activity_end():
                nonlocal activity_open
                nonlocal first_office_turn_complete
                nonlocal office_last_turn_end_at

                if not activity_open:
                    return

                await gemini_session.send_realtime_input(
                    activity_end=types.ActivityEnd()
                )
                activity_open = False
                office_last_turn_end_at = elapsed()

                if not first_office_turn_complete:
                    first_office_turn_complete = True
                    label = "INITIAL OFFICE TURN END"
                else:
                    label = "Office activity END"

                write_diagnostic(
                    diagnostics_path,
                    (
                        f"{label} at {office_last_turn_end_at:.1f}s. "
                        "Gemini may now answer."
                    ),
                )

            async def send_audio_to_gemini(pcm_16k: bytes):
                await gemini_session.send_realtime_input(
                    audio=types.Blob(
                        data=pcm_16k,
                        mime_type="audio/pcm;rate=16000",
                    )
                )

            async def replay_deferred_office_audio():
                nonlocal deferred_audio
                nonlocal office_speaking
                nonlocal office_last_voice_at

                if not deferred_audio:
                    return

                items = list(deferred_audio)
                deferred_audio.clear()

                speech_indices = [
                    i
                    for i, (_, rms) in enumerate(items)
                    if rms >= SPEECH_RMS_THRESHOLD
                ]

                if not speech_indices:
                    write_diagnostic(
                        diagnostics_path,
                        "Discarded deferred office audio containing only silence.",
                    )
                    return

                first = max(0, speech_indices[0] - PREBUFFER_CHUNKS)
                last = min(
                    len(items),
                    speech_indices[-1] + PREBUFFER_CHUNKS + 1,
                )
                selected = items[first:last]

                write_diagnostic(
                    diagnostics_path,
                    (
                        "Replaying deferred office speech after patient playback: "
                        f"{len(selected)} chunks."
                    ),
                )

                await send_activity_start()

                for pcm_16k, _ in selected:
                    await send_audio_to_gemini(pcm_16k)

                # Treat the buffered interruption as one completed office turn.
                # If PGAI is still speaking, fresh live audio will open the next turn.
                await send_activity_end()

                office_speaking = False
                office_last_voice_at = None

            async def send_twilio_audio(mulaw_bytes: bytes):
                """Only code path that sends patient media to Twilio."""
                nonlocal outbound_twilio_bytes

                if not mulaw_bytes:
                    return

                if not (
                    stream_sid
                    and stream_active.is_set()
                ):
                    return

                payload = base64.b64encode(mulaw_bytes).decode("ascii")
                await websocket.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": payload,
                            },
                        }
                    )
                )
                outbound_twilio_bytes += len(mulaw_bytes)

            async def drain_outbound_ready_blocks():
                """
                After ~500 ms startup fill, send ~200 ms real-audio blocks
                whenever enough μ-law is buffered. No timer, no silence.
                """
                nonlocal outbound_startup_ready

                if not outbound_startup_ready:
                    if len(outbound_buffer) < STARTUP_BUFFER_BYTES:
                        return

                    outbound_startup_ready = True
                    startup_wait = 0.0
                    if patient_output_started_at is not None:
                        startup_wait = (
                            elapsed() - patient_output_started_at
                        )

                    write_diagnostic(
                        diagnostics_path,
                        (
                            "Startup buffer ready: "
                            f"{len(outbound_buffer)} bytes after "
                            f"{startup_wait:.3f}s"
                        ),
                    )

                while len(outbound_buffer) >= SEND_BLOCK_BYTES:
                    block = bytes(
                        outbound_buffer[:SEND_BLOCK_BYTES]
                    )
                    del outbound_buffer[:SEND_BLOCK_BYTES]
                    await send_twilio_audio(block)

            async def flush_outbound_turn_and_mark():
                """
                Send any remaining real μ-law, then the Twilio playback mark.
                """
                nonlocal patient_generation_active
                nonlocal patient_output_started_at
                nonlocal patient_turn_number
                nonlocal pending_playback_mark

                if outbound_buffer:
                    remaining = bytes(outbound_buffer)
                    outbound_buffer.clear()
                    await send_twilio_audio(remaining)

                write_diagnostic(
                    diagnostics_path,
                    (
                        "Patient turn flushed. "
                        f"Largest Gemini chunk gap: "
                        f"{outbound_max_gemini_gap_ms:.0f} ms. "
                        f"Gemini audio bytes received: "
                        f"{outbound_gemini_bytes}. "
                        f"Twilio audio bytes sent: "
                        f"{outbound_twilio_bytes}."
                    ),
                )

                patient_turn_number += 1
                mark_name = f"patient-turn-{patient_turn_number}"
                pending_playback_mark = mark_name

                if stream_sid and stream_active.is_set():
                    await websocket.send_text(
                        json.dumps(
                            {
                                "event": "mark",
                                "streamSid": stream_sid,
                                "mark": {
                                    "name": mark_name,
                                },
                            }
                        )
                    )

                write_diagnostic(
                    diagnostics_path,
                    (
                        "Patient generation complete; waiting "
                        f"for Twilio playback mark {mark_name}."
                    ),
                )

                patient_generation_active = False
                patient_output_started_at = None
                reset_outbound_turn_state()

            async def receive_twilio():
                nonlocal stream_sid
                nonlocal twilio_media_chunks
                nonlocal office_speaking
                nonlocal office_speech_seen
                nonlocal office_speech_started_at
                nonlocal office_last_voice_at
                nonlocal patient_playback_active
                nonlocal pending_playback_mark

                try:
                    while True:
                        raw = await websocket.receive_text()
                        message = json.loads(raw)
                        event = message.get("event")

                        if event == "connected":
                            write_diagnostic(
                                diagnostics_path,
                                "Twilio sent 'connected' event.",
                            )

                        elif event == "start":
                            start = message.get("start", {})
                            stream_sid = (
                                start.get("streamSid")
                                or message.get("streamSid")
                            )
                            stream_active.set()

                            write_diagnostic(
                                diagnostics_path,
                                f"Twilio stream started: {stream_sid}",
                            )
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    "Twilio start payload: "
                                    + json.dumps(start, default=str)
                                ),
                            )

                            with transcript_path.open(
                                "a",
                                encoding="utf-8",
                            ) as file:
                                file.write(
                                    f"Twilio Stream SID: {stream_sid}\n\n"
                                )

                        elif event == "media":
                            twilio_media_chunks += 1

                            payload = message["media"]["payload"]
                            mulaw_audio = base64.b64decode(payload)

                            pcm_8k = audioop.ulaw2lin(
                                mulaw_audio,
                                2,
                            )
                            rms = audioop.rms(
                                pcm_8k,
                                2,
                            )

                            pcm_16k = converter.twilio_to_gemini(
                                mulaw_audio
                            )

                            now = elapsed()
                            is_speech = rms >= SPEECH_RMS_THRESHOLD

                            # Always keep a short rolling prefix.
                            prebuffer.append((pcm_16k, rms))

                            if is_speech:
                                office_speech_seen = True
                                office_last_voice_at = now

                                if not office_speaking:
                                    office_speaking = True
                                    office_speech_started_at = now
                                    write_diagnostic(
                                        diagnostics_path,
                                        (
                                            f"Office speech START at {now:.1f}s "
                                            f"(rms={rms})."
                                        ),
                                    )

                            # Do not let PGAI audio reach Gemini while the patient is
                            # still physically playing through Twilio.
                            if patient_playback_active:
                                deferred_audio.append(
                                    (pcm_16k, rms)
                                )

                            else:
                                if is_speech and not activity_open:
                                    await send_activity_start()

                                    # Prefix audio helps preserve first syllables.
                                    prefix_items = list(prebuffer)[
                                        :-1
                                    ]
                                    for prefix_pcm, _ in prefix_items:
                                        await send_audio_to_gemini(
                                            prefix_pcm
                                        )

                                if activity_open:
                                    await send_audio_to_gemini(
                                        pcm_16k
                                    )

                            if (
                                office_speaking
                                and office_last_voice_at is not None
                            ):
                                silence = now - office_last_voice_at

                                required_silence = (
                                    INITIAL_GREETING_END_SILENCE_SECONDS
                                    if not first_office_turn_complete
                                    else NORMAL_END_SILENCE_SECONDS
                                )

                                minimum_time_ok = (
                                    first_office_turn_complete
                                    or now >= INITIAL_GREETING_MIN_SECONDS
                                )

                                if (
                                    silence >= required_silence
                                    and minimum_time_ok
                                ):
                                    office_speaking = False

                                    write_diagnostic(
                                        diagnostics_path,
                                        (
                                            f"Office speech END at "
                                            f"{office_last_voice_at:.1f}s "
                                            f"after {silence:.1f}s silence."
                                        ),
                                    )

                                    # Only close the Gemini office turn when the
                                    # patient is not currently playing.
                                    if (
                                        not patient_playback_active
                                        and activity_open
                                    ):
                                        await send_activity_end()

                            if (
                                twilio_media_chunks % 500
                                == 0
                            ):
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Inbound media healthy: "
                                        f"{twilio_media_chunks} chunks "
                                        f"at {now:.1f}s."
                                    ),
                                )

                        elif event == "mark":
                            mark = message.get("mark", {})
                            name = mark.get("name")

                            write_diagnostic(
                                diagnostics_path,
                                f"Twilio playback MARK received: {name}",
                            )

                            if (
                                pending_playback_mark
                                and name == pending_playback_mark
                            ):
                                patient_playback_active = False
                                pending_playback_mark = None

                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Patient audio has physically finished "
                                        "playing through Twilio."
                                    ),
                                )

                                await replay_deferred_office_audio()

                        elif event == "stop":
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    f"Twilio sent 'stop' event at "
                                    f"{elapsed():.1f}s."
                                ),
                            )
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    "FULL TWILIO STOP MESSAGE: "
                                    + json.dumps(
                                        message,
                                        default=str,
                                    )
                                ),
                            )

                            stream_active.clear()
                            await flush_remaining()
                            return "twilio_stop"

                except WebSocketDisconnect as error:
                    stream_active.clear()
                    write_diagnostic(
                        diagnostics_path,
                        (
                            "Twilio WebSocket disconnected at "
                            f"{elapsed():.1f}s. "
                            f"Code={getattr(error, 'code', None)}"
                        ),
                    )
                    await flush_remaining()
                    return "twilio_websocket_disconnect"

                except asyncio.CancelledError:
                    write_diagnostic(
                        diagnostics_path,
                        "Twilio receive task cancelled.",
                    )
                    raise

                except Exception as error:
                    stream_active.clear()
                    write_diagnostic(
                        diagnostics_path,
                        (
                            "TWILIO RECEIVE ERROR: "
                            f"{type(error).__name__}: {error}"
                        ),
                    )
                    write_diagnostic(
                        diagnostics_path,
                        traceback.format_exc(),
                    )
                    await flush_remaining()
                    return "twilio_receive_error"

            async def receive_gemini():
                nonlocal gemini_audio_chunks
                nonlocal patient_generation_active
                nonlocal patient_playback_active
                nonlocal patient_output_started_at
                nonlocal outbound_gemini_bytes
                nonlocal outbound_last_gemini_chunk_at
                nonlocal outbound_max_gemini_gap_ms

                try:
                    while True:
                        async for response in gemini_session.receive():
                            content = response.server_content

                            if not content:
                                continue

                            if getattr(
                                content,
                                "interrupted",
                                False,
                            ):
                                # With manual VAD, this should normally disappear.
                                # If it occurs, log it loudly. Never clear Twilio.
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "UNEXPECTED Gemini interruption at "
                                        f"{elapsed():.1f}s under MANUAL VAD."
                                    ),
                                )

                            input_transcription = getattr(
                                content,
                                "input_transcription",
                                None,
                            )

                            if input_transcription:
                                text = getattr(
                                    input_transcription,
                                    "text",
                                    None,
                                )
                                if text:
                                    office_buffer.append(text)

                            output_transcription = getattr(
                                content,
                                "output_transcription",
                                None,
                            )

                            if output_transcription:
                                if office_buffer:
                                    append_transcript(
                                        transcript_path,
                                        "Office",
                                        office_buffer,
                                    )
                                    office_buffer.clear()

                                text = getattr(
                                    output_transcription,
                                    "text",
                                    None,
                                )
                                if text:
                                    patient_buffer.append(text)

                            model_turn = getattr(
                                content,
                                "model_turn",
                                None,
                            )

                            if model_turn:
                                for part in model_turn.parts:
                                    if not (
                                        part.inline_data
                                        and part.inline_data.data
                                    ):
                                        continue

                                    if not (
                                        stream_sid
                                        and stream_active.is_set()
                                    ):
                                        continue

                                    now = elapsed()

                                    if not patient_generation_active:
                                        patient_generation_active = True
                                        patient_playback_active = True
                                        patient_output_started_at = now
                                        reset_outbound_turn_state()

                                        latency_note = ""
                                        if office_last_turn_end_at is not None:
                                            latency = (
                                                patient_output_started_at
                                                - office_last_turn_end_at
                                            )
                                            latency_note = (
                                                " Response latency: "
                                                f"{latency:.1f}s."
                                            )

                                        write_diagnostic(
                                            diagnostics_path,
                                            (
                                                "Patient output START at "
                                                f"{patient_output_started_at:.1f}s."
                                                + latency_note
                                            ),
                                        )
                                        write_diagnostic(
                                            diagnostics_path,
                                            "Patient audio first Gemini chunk",
                                        )

                                    if outbound_last_gemini_chunk_at is not None:
                                        gap_ms = (
                                            now
                                            - outbound_last_gemini_chunk_at
                                        ) * 1000.0
                                        if gap_ms > outbound_max_gemini_gap_ms:
                                            outbound_max_gemini_gap_ms = gap_ms
                                    outbound_last_gemini_chunk_at = now

                                    pcm_24k = part.inline_data.data
                                    mulaw_8k = converter.gemini_to_twilio(
                                        pcm_24k
                                    )
                                    outbound_buffer.extend(mulaw_8k)
                                    outbound_gemini_bytes += len(pcm_24k)
                                    gemini_audio_chunks += 1

                                    await drain_outbound_ready_blocks()

                            if getattr(
                                content,
                                "turn_complete",
                                False,
                            ):
                                if patient_generation_active:
                                    output_end = elapsed()
                                    duration = (
                                        output_end
                                        - patient_output_started_at
                                        if patient_output_started_at
                                        is not None
                                        else 0.0
                                    )

                                    write_diagnostic(
                                        diagnostics_path,
                                        (
                                            "Patient generation END at "
                                            f"{output_end:.1f}s "
                                            f"(turn={duration:.1f}s)."
                                        ),
                                    )

                                    await flush_outbound_turn_and_mark()

                                if patient_buffer:
                                    append_transcript(
                                        transcript_path,
                                        "Patient",
                                        patient_buffer,
                                    )
                                    patient_buffer.clear()

                except asyncio.CancelledError:
                    write_diagnostic(
                        diagnostics_path,
                        "Gemini receive task cancelled.",
                    )
                    raise

                except Exception as error:
                    write_diagnostic(
                        diagnostics_path,
                        (
                            "GEMINI RECEIVE ERROR at "
                            f"{elapsed():.1f}s: "
                            f"{type(error).__name__}: {error}"
                        ),
                    )
                    write_diagnostic(
                        diagnostics_path,
                        traceback.format_exc(),
                    )
                    await flush_remaining()
                    return "gemini_receive_error"

            twilio_task = asyncio.create_task(
                receive_twilio(),
                name="twilio-receive",
            )
            gemini_task = asyncio.create_task(
                receive_gemini(),
                name="gemini-receive",
            )

            done, pending = await asyncio.wait(
                {
                    twilio_task,
                    gemini_task,
                },
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    result = task.result()
                    write_diagnostic(
                        diagnostics_path,
                        (
                            f"TASK ENDED FIRST: "
                            f"{task.get_name()} result={result}"
                        ),
                    )
                except Exception as error:
                    write_diagnostic(
                        diagnostics_path,
                        (
                            f"TASK FAILED FIRST: {task.get_name()} "
                            f"{type(error).__name__}: {error}"
                        ),
                    )

            for task in pending:
                write_diagnostic(
                    diagnostics_path,
                    (
                        "Cancelling remaining task: "
                        f"{task.get_name()}"
                    ),
                )
                task.cancel()

            await asyncio.gather(
                *pending,
                return_exceptions=True,
            )

            await flush_remaining()

    except Exception as error:
        write_diagnostic(
            diagnostics_path,
            (
                "CALL SESSION ERROR: "
                f"{type(error).__name__}: {error}"
            ),
        )
        write_diagnostic(
            diagnostics_path,
            traceback.format_exc(),
        )

    finally:
        stream_active.clear()

        write_diagnostic(
            diagnostics_path,
            (
                "Call session closed. "
                f"Elapsed={elapsed():.1f}s, "
                f"Twilio chunks={twilio_media_chunks}, "
                f"Gemini audio chunks={gemini_audio_chunks}."
            ),
        )

        print(
            "Call session closed cleanly.",
            flush=True,
        )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )
