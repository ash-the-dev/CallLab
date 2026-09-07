"""
OpenAI Realtime provider for CallLab.

Twilio ↔ CallLab ↔ OpenAI Realtime (WebSocket).

Uses μ-law/8000 (audio/pcmu) both directions. CallLab owns office-turn
segmentation: OpenAI server_vad is disabled so input is committed only when
an office turn ends. Each completed office_turn_id gets at most one
response.create (plus one watchdog retry if OpenAI never acknowledges).
A response is not treated as active until OpenAI confirms it.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_MODEL,
    OPENAI_REALTIME_VOICE,
)


OPENAI_REALTIME_URL = (
    f"wss://api.openai.com/v1/realtime"
    f"?model={OPENAI_REALTIME_MODEL}"
)

SPEECH_RMS_THRESHOLD = 350
# Latest self-test still false-ended mid-utterance at ~1.15s (logged 1.2s).
NORMAL_END_SILENCE_SECONDS = 0.90
# Debounce after silence threshold before commit/response.create.
OFFICE_TURN_END_GRACE_SECONDS = 0.30
# Voiced speech shorter than this is held for merge, not answered.
MIN_OFFICE_TURN_SPEECH_SECONDS = 1.00
INITIAL_GREETING_MIN_SECONDS = 12.0
INITIAL_GREETING_END_SILENCE_SECONDS = 1.20
RESPONSE_START_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_CREATE_RETRIES = 1

TWILIO_CHUNKS_PER_SECOND = 50
DEFERRED_AUDIO_MAX_SECONDS = 12
DEFERRED_AUDIO_MAX_CHUNKS = (
    DEFERRED_AUDIO_MAX_SECONDS * TWILIO_CHUNKS_PER_SECOND
)


def write_diagnostic(path: Path, message: str):
    timestamp = datetime.now().isoformat(timespec="milliseconds")
    line = f"[{timestamp}] {message}"

    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")

    print(f"[DIAG] {message}", flush=True)


def clean_fragments(fragments: list[str]) -> str:
    import re

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


async def run_openai_media_stream(
    websocket: WebSocket,
    scenario: dict,
    patient_prompt: str,
    transcript_path: Path,
    diagnostics_path: Path,
):
    write_diagnostic(
        diagnostics_path,
        (
            "Opening OpenAI Realtime session with "
            f"{OPENAI_REALTIME_MODEL} voice={OPENAI_REALTIME_VOICE}."
        ),
    )

    stream_sid = None
    stream_active = asyncio.Event()

    office_buffer: list[str] = []
    patient_buffer: list[str] = []

    first_office_turn_complete = False
    office_speaking = False
    office_speech_seen = False
    office_speech_started_at = None
    office_last_voice_at = None
    office_last_turn_end_at = None
    response_allowed = False

    # Explicit turn-state machine.
    # requested = response.create SENT (not yet OpenAI-confirmed)
    # created = response.created ACK from OpenAI
    # active_audio = first output audio delta received
    office_turn_id = 0
    active_office_turn_id = None
    latest_started_office_turn_id = 0
    latest_completed_office_turn_id = 0
    pending_response_turn_id = None
    response_requested_turn_id = None
    response_created_turn_id = None
    active_audio_turn_id = None
    active_openai_response_id = None
    allowed_play_response_id = None
    blocked_response_ids: set[str] = set()
    last_completed_response_turn_id = 0
    response_retry_count: dict[int, int] = {}
    response_watchdog_task = None
    # Turns where OpenAI completed a response with no patient audio
    # (intentional silence / nothing to say). Must not watchdog-retry.
    intentional_silent_turns: set[int] = set()
    office_turn_stats: dict[int, dict] = {}

    # End-of-turn debounce / short-fragment hold.
    pending_office_end_since = None
    holding_short_office_turn = False

    patient_generation_active = False
    patient_playback_active = False
    patient_output_started_at = None
    patient_turn_number = 0
    pending_playback_mark = None

    outbound_openai_bytes = 0
    outbound_twilio_bytes = 0
    outbound_last_delta_at = None
    outbound_max_delta_gap_ms = 0.0
    openai_audio_deltas = 0

    # (mulaw, rms, office_turn_id)
    deferred_audio = deque(maxlen=DEFERRED_AUDIO_MAX_CHUNKS)

    twilio_media_chunks = 0

    session_start = asyncio.get_running_loop().time()

    def elapsed() -> float:
        return asyncio.get_running_loop().time() - session_start

    def reset_patient_turn_stats():
        nonlocal outbound_openai_bytes
        nonlocal outbound_twilio_bytes
        nonlocal outbound_last_delta_at
        nonlocal outbound_max_delta_gap_ms
        nonlocal openai_audio_deltas

        outbound_openai_bytes = 0
        outbound_twilio_bytes = 0
        outbound_last_delta_at = None
        outbound_max_delta_gap_ms = 0.0
        openai_audio_deltas = 0

    def ensure_turn_stats(turn_id: int) -> dict:
        stats = office_turn_stats.get(turn_id)
        if stats is None:
            stats = {
                "received": 0,
                "sent_live": 0,
                "deferred": 0,
                "replayed": 0,
                "response_create": 0,
            }
            office_turn_stats[turn_id] = stats
        return stats

    def log_office_turn_stats(turn_id: int, label: str):
        stats = ensure_turn_stats(turn_id)
        write_diagnostic(
            diagnostics_path,
            (
                f"{label} office turn {turn_id} stats: "
                f"received={stats['received']} "
                f"sent_live={stats['sent_live']} "
                f"deferred={stats['deferred']} "
                f"replayed={stats['replayed']} "
                f"response_create={stats['response_create']} "
                f"response_retry={response_retry_count.get(turn_id, 0)}"
            ),
        )

    def log_turn_state(note: str):
        write_diagnostic(
            diagnostics_path,
            (
                f"TURN STATE ({note}): "
                f"started={latest_started_office_turn_id} "
                f"completed={latest_completed_office_turn_id} "
                f"pending_response={pending_response_turn_id} "
                f"requested={response_requested_turn_id} "
                f"created={response_created_turn_id} "
                f"active_audio={active_audio_turn_id} "
                f"response_id={active_openai_response_id} "
                f"last_completed_response={last_completed_response_turn_id}"
            ),
        )

    def clear_response_request_state(turn_id: int | None = None):
        nonlocal response_requested_turn_id
        nonlocal response_created_turn_id
        nonlocal active_audio_turn_id
        nonlocal active_openai_response_id
        nonlocal allowed_play_response_id

        if (
            turn_id is not None
            and response_requested_turn_id is not None
            and response_requested_turn_id != turn_id
        ):
            return

        if active_openai_response_id:
            blocked_response_ids.add(active_openai_response_id)
        if allowed_play_response_id:
            blocked_response_ids.add(allowed_play_response_id)

        response_requested_turn_id = None
        response_created_turn_id = None
        active_audio_turn_id = None
        active_openai_response_id = None
        allowed_play_response_id = None

    def cancel_response_watchdog():
        nonlocal response_watchdog_task

        task = response_watchdog_task
        response_watchdog_task = None
        if task is not None and not task.done():
            task.cancel()

    def openai_response_is_cancellable() -> bool:
        """True only after OpenAI has acknowledged a response."""
        return (
            response_created_turn_id is not None
            or active_audio_turn_id is not None
            or patient_generation_active
        )

    def office_turn_speech_seconds() -> float:
        if (
            office_speech_started_at is None
            or office_last_voice_at is None
        ):
            return 0.0
        return max(0.0, office_last_voice_at - office_speech_started_at)

    def cancel_pending_office_end(reason: str):
        nonlocal pending_office_end_since

        if pending_office_end_since is None:
            return
        write_diagnostic(
            diagnostics_path,
            (
                f"CANCEL PENDING OFFICE END turn="
                f"{active_office_turn_id}: {reason}."
            ),
        )
        pending_office_end_since = None


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

    async def send_twilio_audio(mulaw_bytes: bytes):
        nonlocal outbound_twilio_bytes

        if not mulaw_bytes:
            return

        if not (stream_sid and stream_active.is_set()):
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

    async def send_openai_event(ws, payload: dict):
        await ws.send(json.dumps(payload))

    async def append_office_mulaw(ws, mulaw_8k: bytes):
        await send_openai_event(
            ws,
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(mulaw_8k).decode("ascii"),
            },
        )

    async def clear_openai_input_buffer(ws):
        await send_openai_event(
            ws,
            {
                "type": "input_audio_buffer.clear",
            },
        )

    async def commit_openai_input_buffer(ws):
        await send_openai_event(
            ws,
            {
                "type": "input_audio_buffer.commit",
            },
        )

    async def cancel_openai_response(ws, reason: str):
        nonlocal patient_generation_active

        turn_id = (
            active_audio_turn_id
            or response_created_turn_id
            or response_requested_turn_id
        )

        if not openai_response_is_cancellable():
            write_diagnostic(
                diagnostics_path,
                (
                    "Skip RESPONSE CANCEL "
                    f"turn={turn_id} ({reason}): "
                    "OpenAI never acknowledged an active response."
                ),
            )
            cancel_response_watchdog()
            clear_response_request_state(turn_id)
            patient_generation_active = False
            log_turn_state("after-local-invalidate")
            return

        await send_openai_event(
            ws,
            {
                "type": "response.cancel",
            },
        )
        write_diagnostic(
            diagnostics_path,
            (
                f"RESPONSE CANCEL turn={turn_id} ({reason}) "
                f"at {elapsed():.1f}s "
                f"response_id={active_openai_response_id}."
            ),
        )
        cancel_response_watchdog()
        clear_response_request_state(turn_id)
        patient_generation_active = False
        log_turn_state("after-response-cancel")

    async def send_response_create_event(
        ws,
        turn_id: int,
        reason: str,
        *,
        is_retry: bool = False,
    ):
        nonlocal response_requested_turn_id

        await send_openai_event(
            ws,
            {
                "type": "response.create",
            },
        )

        response_requested_turn_id = turn_id
        if not is_retry:
            ensure_turn_stats(turn_id)["response_create"] += 1

        label = (
            "RETRY RESPONSE CREATE"
            if is_retry
            else "RESPONSE CREATE SENT"
        )
        if is_retry:
            write_diagnostic(
                diagnostics_path,
                (
                    f"{label} turn={turn_id} "
                    f"attempt={response_retry_count.get(turn_id, 0)}"
                ),
            )
        else:
            write_diagnostic(
                diagnostics_path,
                (
                    f"{label} turn={turn_id} "
                    f"({reason}) at {elapsed():.1f}s."
                ),
            )
        log_office_turn_stats(
            turn_id,
            "After retry response.create"
            if is_retry
            else "After response.create",
        )
        log_turn_state(
            "after-retry-response-create"
            if is_retry
            else "after-response-create-sent"
        )
        start_response_watchdog(ws, turn_id)

    async def response_start_watchdog(ws, turn_id: int):
        try:
            await asyncio.sleep(RESPONSE_START_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return

        # Intentional silent completion already finished this turn.
        if turn_id in intentional_silent_turns:
            return

        # Still only a local request — OpenAI never created/started audio.
        if response_requested_turn_id != turn_id:
            return
        if response_created_turn_id == turn_id:
            # Acknowledged but not yet spoken: wait for response.done /
            # audio. Do not treat as a no-start failure.
            return
        if active_audio_turn_id == turn_id:
            return
        if patient_generation_active:
            return
        if turn_id != latest_completed_office_turn_id:
            return
        if latest_started_office_turn_id > turn_id:
            return

        write_diagnostic(
            diagnostics_path,
            f"RESPONSE START TIMEOUT turn={turn_id}",
        )
        log_turn_state("response-start-timeout")

        retries = response_retry_count.get(turn_id, 0)
        if retries >= MAX_RESPONSE_CREATE_RETRIES:
            write_diagnostic(
                diagnostics_path,
                (
                    "RESPONSE START TIMEOUT exhausted retries "
                    f"turn={turn_id}; leaving request invalidated."
                ),
            )
            clear_response_request_state(turn_id)
            log_turn_state("response-timeout-exhausted")
            return

        # Conservative retry: still no created/audio/done, same latest turn.
        response_retry_count[turn_id] = retries + 1
        clear_response_request_state(turn_id)
        await send_response_create_event(
            ws,
            turn_id,
            "response-start-watchdog",
            is_retry=True,
        )

    def start_response_watchdog(ws, turn_id: int):
        nonlocal response_watchdog_task

        cancel_response_watchdog()
        response_watchdog_task = asyncio.create_task(
            response_start_watchdog(ws, turn_id),
            name=f"response-start-watchdog-{turn_id}",
        )

    async def create_response_for_office_turn(ws, turn_id: int, reason: str):
        nonlocal pending_response_turn_id

        if not response_allowed:
            return

        if turn_id is None:
            return

        if turn_id != latest_completed_office_turn_id:
            write_diagnostic(
                diagnostics_path,
                (
                    "Skipped response.create: "
                    f"turn={turn_id} is not latest completed "
                    f"({latest_completed_office_turn_id}) ({reason})."
                ),
            )
            log_turn_state("skip-not-latest-completed")
            return

        stats = ensure_turn_stats(turn_id)
        if stats["response_create"] >= 1:
            write_diagnostic(
                diagnostics_path,
                (
                    "Skipped duplicate response.create for office turn "
                    f"{turn_id} ({reason})."
                ),
            )
            return

        if patient_generation_active or patient_playback_active:
            pending_response_turn_id = turn_id
            write_diagnostic(
                diagnostics_path,
                (
                    "Queued response.create for office turn "
                    f"{turn_id} ({reason}); patient still playing."
                ),
            )
            log_turn_state("queued-pending-response")
            return

        # Commit this office turn's audio before creating a response so OpenAI
        # answers the just-finished turn, not a previous segment.
        await commit_openai_input_buffer(ws)
        write_diagnostic(
            diagnostics_path,
            f"INPUT COMMIT turn={turn_id}",
        )
        write_diagnostic(
            diagnostics_path,
            (
                f"INPUT COMMIT for office turn {turn_id} "
                f"before response.create ({reason})."
            ),
        )

        pending_response_turn_id = None
        await send_response_create_event(
            ws,
            turn_id,
            reason,
            is_retry=False,
        )

    async def invalidate_stale_pending_on_new_office_turn(ws, new_turn_id: int):
        nonlocal pending_response_turn_id

        if (
            pending_response_turn_id is not None
            and pending_response_turn_id < new_turn_id
        ):
            write_diagnostic(
                diagnostics_path,
                (
                    "STALE pending response invalidated: "
                    f"pending_turn={pending_response_turn_id} "
                    f"< new_office_turn={new_turn_id}."
                ),
            )
            pending_response_turn_id = None
            log_turn_state("stale-pending-invalidated")

        stale_turn = response_requested_turn_id
        if stale_turn is None or stale_turn >= new_turn_id:
            return

        cancel_response_watchdog()

        if openai_response_is_cancellable() and (
            response_created_turn_id == stale_turn
            or active_audio_turn_id == stale_turn
            or patient_generation_active
        ):
            await cancel_openai_response(
                ws,
                (
                    f"stale-active-response "
                    f"turn={stale_turn} < office_turn={new_turn_id}"
                ),
            )
            return

        write_diagnostic(
            diagnostics_path,
            (
                f"STALE REQUEST INVALIDATED turn={stale_turn} "
                f"because office turn={new_turn_id} started"
            ),
        )
        clear_response_request_state(stale_turn)
        log_turn_state("stale-request-invalidated")

    async def replay_deferred_office_audio(ws):
        """
        Replay deferred office bytes once after Jane finishes.
        Assigns replayed audio to current/new office turns already tagged
        on each chunk. Does not create responses for older turns when a
        newer completed turn already exists.
        """
        nonlocal deferred_audio

        if not deferred_audio:
            return

        items = list(deferred_audio)
        deferred_audio.clear()

        by_turn: dict[int, list] = {}
        for mulaw_8k, rms, turn_id in items:
            by_turn.setdefault(turn_id, []).append((mulaw_8k, rms))

        for turn_id in sorted(by_turn.keys()):
            turn_items = by_turn[turn_id]
            speech_indices = [
                i
                for i, (_, rms) in enumerate(turn_items)
                if rms >= SPEECH_RMS_THRESHOLD
            ]
            if not speech_indices:
                write_diagnostic(
                    diagnostics_path,
                    (
                        f"Discarded deferred office turn {turn_id} "
                        "containing only silence."
                    ),
                )
                continue

            # Never feed historical deferred audio for a turn older than the
            # latest completed office turn when a newer turn already exists.
            if (
                latest_completed_office_turn_id
                and turn_id < latest_completed_office_turn_id
            ):
                write_diagnostic(
                    diagnostics_path,
                    (
                        "Skipped historical deferred replay for office turn "
                        f"{turn_id}; latest_completed="
                        f"{latest_completed_office_turn_id}."
                    ),
                )
                continue

            stats = ensure_turn_stats(turn_id)
            stats["replayed"] += len(turn_items)

            write_diagnostic(
                diagnostics_path,
                (
                    "Replaying deferred office speech after patient playback: "
                    f"office turn {turn_id}, {len(turn_items)} chunks."
                ),
            )

            for mulaw_8k, _ in turn_items:
                await append_office_mulaw(ws, mulaw_8k)

            log_office_turn_stats(turn_id, "After deferred replay")

    async def finish_patient_turn_and_mark():
        nonlocal patient_generation_active
        nonlocal patient_output_started_at
        nonlocal patient_turn_number
        nonlocal pending_playback_mark
        nonlocal last_completed_response_turn_id

        completed_turn = active_audio_turn_id or response_created_turn_id
        response_id = active_openai_response_id

        write_diagnostic(
            diagnostics_path,
            (
                "Patient turn flushed. "
                f"Largest OpenAI audio-delta gap: "
                f"{outbound_max_delta_gap_ms:.0f} ms. "
                f"OpenAI audio bytes received: "
                f"{outbound_openai_bytes}. "
                f"Twilio audio bytes sent: "
                f"{outbound_twilio_bytes}."
            ),
        )

        if completed_turn is not None:
            write_diagnostic(
                diagnostics_path,
                (
                    f"RESPONSE DONE turn={completed_turn} "
                    f"response_id={response_id}."
                ),
            )
            last_completed_response_turn_id = completed_turn

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
                "Patient response completed; waiting "
                f"for Twilio playback mark {mark_name}."
            ),
        )

        patient_generation_active = False
        patient_output_started_at = None
        cancel_response_watchdog()
        clear_response_request_state(completed_turn)
        reset_patient_turn_stats()
        log_turn_state("after-response-done")

    try:
        async with websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
            max_size=8 * 1024 * 1024,
        ) as openai_ws:
            write_diagnostic(
                diagnostics_path,
                "OpenAI Realtime WebSocket connected.",
            )

            # Disable server_vad so CallLab owns commit boundaries.
            session_update = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": OPENAI_REALTIME_MODEL,
                    "output_modalities": ["audio"],
                    "instructions": patient_prompt,
                    "audio": {
                        "input": {
                            "format": {
                                "type": "audio/pcmu",
                            },
                            "transcription": {
                                "model": "whisper-1",
                            },
                            "turn_detection": None,
                        },
                        "output": {
                            "format": {
                                "type": "audio/pcmu",
                            },
                            "voice": OPENAI_REALTIME_VOICE,
                        },
                    },
                },
            }
            await send_openai_event(openai_ws, session_update)
            write_diagnostic(
                diagnostics_path,
                (
                    "Sent OpenAI session.update "
                    "(pcmu in/out, turn_detection=null, CallLab commits)."
                ),
            )

            async def receive_twilio():
                nonlocal stream_sid
                nonlocal twilio_media_chunks
                nonlocal office_speaking
                nonlocal office_speech_seen
                nonlocal office_speech_started_at
                nonlocal office_last_voice_at
                nonlocal office_last_turn_end_at
                nonlocal first_office_turn_complete
                nonlocal response_allowed
                nonlocal patient_playback_active
                nonlocal pending_playback_mark
                nonlocal office_turn_id
                nonlocal active_office_turn_id
                nonlocal latest_started_office_turn_id
                nonlocal latest_completed_office_turn_id
                nonlocal pending_response_turn_id
                nonlocal pending_office_end_since
                nonlocal holding_short_office_turn

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

                            pcm_8k = audioop.ulaw2lin(mulaw_audio, 2)
                            rms = audioop.rms(pcm_8k, 2)
                            now = elapsed()
                            is_speech = rms >= SPEECH_RMS_THRESHOLD

                            if is_speech:
                                office_speech_seen = True
                                office_last_voice_at = now

                                if pending_office_end_since is not None:
                                    cancel_pending_office_end(
                                        "office speech resumed during grace"
                                    )

                                if not office_speaking:
                                    office_speaking = True

                                    if (
                                        holding_short_office_turn
                                        and active_office_turn_id is not None
                                    ):
                                        # Continue the same office turn; do not
                                        # clear buffer or mint a new turn id.
                                        holding_short_office_turn = False
                                        write_diagnostic(
                                            diagnostics_path,
                                            (
                                                "RESUME OFFICE TURN "
                                                f"{active_office_turn_id} "
                                                "after short-fragment hold "
                                                f"at {now:.1f}s (rms={rms})."
                                            ),
                                        )
                                        log_turn_state(
                                            "office-turn-resume-merge"
                                        )
                                    else:
                                        office_speech_started_at = now
                                        office_turn_id += 1
                                        active_office_turn_id = office_turn_id
                                        latest_started_office_turn_id = (
                                            office_turn_id
                                        )
                                        ensure_turn_stats(
                                            active_office_turn_id
                                        )

                                        await invalidate_stale_pending_on_new_office_turn(
                                            openai_ws,
                                            active_office_turn_id,
                                        )
                                        await clear_openai_input_buffer(
                                            openai_ws
                                        )

                                        write_diagnostic(
                                            diagnostics_path,
                                            (
                                                f"OFFICE TURN {active_office_turn_id} "
                                                f"START at {now:.1f}s (rms={rms})."
                                            ),
                                        )
                                        log_turn_state("office-turn-start")

                            if active_office_turn_id is not None:
                                ensure_turn_stats(
                                    active_office_turn_id
                                )["received"] += 1

                                # XOR: live during turn while Jane is idle,
                                # deferred while Jane is playing.
                                if patient_playback_active:
                                    deferred_audio.append(
                                        (
                                            mulaw_audio,
                                            rms,
                                            active_office_turn_id,
                                        )
                                    )
                                    ensure_turn_stats(
                                        active_office_turn_id
                                    )["deferred"] += 1
                                else:
                                    ensure_turn_stats(
                                        active_office_turn_id
                                    )["sent_live"] += 1
                                    await append_office_mulaw(
                                        openai_ws,
                                        mulaw_audio,
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

                                greeting_ready = (
                                    first_office_turn_complete
                                    or (
                                        office_speech_seen
                                        and now
                                        >= INITIAL_GREETING_MIN_SECONDS
                                    )
                                )

                                if (
                                    silence >= required_silence
                                    and greeting_ready
                                ):
                                    if pending_office_end_since is None:
                                        pending_office_end_since = now
                                        write_diagnostic(
                                            diagnostics_path,
                                            (
                                                "PENDING OFFICE END "
                                                f"turn={active_office_turn_id} "
                                                f"after {silence:.1f}s silence; "
                                                f"grace={OFFICE_TURN_END_GRACE_SECONDS:.2f}s."
                                            ),
                                        )

                                    grace_elapsed = (
                                        now - pending_office_end_since
                                    )
                                    if (
                                        grace_elapsed
                                        < OFFICE_TURN_END_GRACE_SECONDS
                                    ):
                                        pass
                                    else:
                                        speech_secs = (
                                            office_turn_speech_seconds()
                                        )
                                        ended_turn_id = (
                                            active_office_turn_id
                                        )
                                        pending_office_end_since = None

                                        # Tiny fragments ("You...", "OK.") must
                                        # not trigger a patient response.
                                        if (
                                            first_office_turn_complete
                                            and speech_secs
                                            < MIN_OFFICE_TURN_SPEECH_SECONDS
                                        ):
                                            office_speaking = False
                                            holding_short_office_turn = True
                                            write_diagnostic(
                                                diagnostics_path,
                                                (
                                                    "HOLD SHORT OFFICE TURN "
                                                    f"{ended_turn_id} for merge "
                                                    f"(speech={speech_secs:.2f}s "
                                                    f"< {MIN_OFFICE_TURN_SPEECH_SECONDS:.2f}s); "
                                                    "no commit/response.create."
                                                ),
                                            )
                                            log_turn_state(
                                                "hold-short-office-turn"
                                            )
                                        else:
                                            office_speaking = False
                                            holding_short_office_turn = False
                                            office_last_turn_end_at = (
                                                office_last_voice_at
                                            )
                                            latest_completed_office_turn_id = (
                                                ended_turn_id
                                            )

                                            write_diagnostic(
                                                diagnostics_path,
                                                (
                                                    f"OFFICE TURN {ended_turn_id} END at "
                                                    f"{now:.1f}s after "
                                                    f"{silence:.1f}s silence "
                                                    f"(speech={speech_secs:.2f}s, "
                                                    f"grace={grace_elapsed:.2f}s)."
                                                ),
                                            )
                                            log_office_turn_stats(
                                                ended_turn_id,
                                                "At END",
                                            )
                                            log_turn_state(
                                                "office-turn-end"
                                            )

                                            if not first_office_turn_complete:
                                                first_office_turn_complete = (
                                                    True
                                                )
                                                response_allowed = True
                                                await create_response_for_office_turn(
                                                    openai_ws,
                                                    ended_turn_id,
                                                    "initial-greeting-complete",
                                                )
                                            else:
                                                await create_response_for_office_turn(
                                                    openai_ws,
                                                    ended_turn_id,
                                                    "office-turn-end",
                                                )

                                            active_office_turn_id = None
                                else:
                                    if pending_office_end_since is not None:
                                        cancel_pending_office_end(
                                            "silence fell below threshold"
                                        )

                            if twilio_media_chunks % 500 == 0:
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Inbound media healthy: "
                                        f"{twilio_media_chunks} chunks "
                                        f"at {elapsed():.1f}s."
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

                                await replay_deferred_office_audio(
                                    openai_ws
                                )

                                if pending_response_turn_id is not None:
                                    await create_response_for_office_turn(
                                        openai_ws,
                                        pending_response_turn_id,
                                        "after-mark",
                                    )

                        elif event == "stop":
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    "Twilio sent 'stop' event at "
                                    f"{elapsed():.1f}s."
                                ),
                            )
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    "FULL TWILIO STOP MESSAGE: "
                                    + json.dumps(message, default=str)
                                ),
                            )
                            stream_active.clear()
                            await flush_remaining()
                            return "twilio_stop"

                        else:
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    "Unknown Twilio event: "
                                    f"{event} "
                                    + json.dumps(message, default=str)
                                ),
                            )

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

            async def receive_openai():
                nonlocal patient_generation_active
                nonlocal patient_playback_active
                nonlocal patient_output_started_at
                nonlocal outbound_openai_bytes
                nonlocal outbound_last_delta_at
                nonlocal outbound_max_delta_gap_ms
                nonlocal openai_audio_deltas
                nonlocal response_requested_turn_id
                nonlocal response_created_turn_id
                nonlocal active_audio_turn_id
                nonlocal active_openai_response_id
                nonlocal allowed_play_response_id
                nonlocal last_completed_response_turn_id

                try:
                    async for raw in openai_ws:
                        message = json.loads(raw)
                        event = message.get("type")

                        if event in {
                            "session.created",
                            "session.updated",
                        }:
                            write_diagnostic(
                                diagnostics_path,
                                f"OpenAI event: {event}",
                            )

                        elif event == "input_audio_buffer.committed":
                            item_id = message.get("item_id")
                            turn_for_commit = (
                                response_requested_turn_id
                                or latest_completed_office_turn_id
                            )
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    f"INPUT COMMITTED turn={turn_for_commit} "
                                    f"item_id={item_id}."
                                ),
                            )

                        elif event == "response.created":
                            response_obj = message.get("response") or {}
                            response_id = response_obj.get("id")

                            if response_requested_turn_id is None:
                                # Late/orphaned create after local invalidate.
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "UNEXPECTED RESPONSE CREATED "
                                        f"response_id={response_id} "
                                        "(no local request); cancelling."
                                    ),
                                )
                                await send_openai_event(
                                    openai_ws,
                                    {"type": "response.cancel"},
                                )
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "RESPONSE CANCEL orphaned "
                                        f"response_id={response_id}."
                                    ),
                                )
                                continue

                            turn_for_created = response_requested_turn_id
                            response_created_turn_id = turn_for_created
                            active_openai_response_id = response_id
                            allowed_play_response_id = response_id
                            if response_id in blocked_response_ids:
                                blocked_response_ids.discard(response_id)
                            cancel_response_watchdog()
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    f"RESPONSE CREATED turn={turn_for_created} "
                                    f"response_id={response_id}."
                                ),
                            )
                            log_turn_state("response-created")

                        elif event in {
                            "response.output_audio.delta",
                            "response.audio.delta",
                        }:
                            delta_b64 = (
                                message.get("delta")
                                or message.get("audio")
                            )
                            if not delta_b64:
                                continue

                            if not (
                                stream_sid and stream_active.is_set()
                            ):
                                continue

                            delta_response_id = message.get("response_id")
                            response_turn = (
                                active_audio_turn_id
                                or response_created_turn_id
                                or response_requested_turn_id
                            )

                            # Never play orphan/superseded audio (no turn=None).
                            if (
                                delta_response_id
                                and delta_response_id in blocked_response_ids
                            ):
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Blocked BLOCKED-ID AUDIO "
                                        f"response_id={delta_response_id}."
                                    ),
                                )
                                continue

                            if allowed_play_response_id is None or (
                                delta_response_id
                                and delta_response_id
                                != allowed_play_response_id
                            ):
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Blocked ORPHAN/MISMATCH AUDIO "
                                        f"turn={response_turn} "
                                        f"response_id={delta_response_id} "
                                        f"allowed={allowed_play_response_id}."
                                    ),
                                )
                                continue

                            if response_turn is None:
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Blocked turn=None AUDIO "
                                        f"response_id={delta_response_id}."
                                    ),
                                )
                                continue

                            # Drop audio from a stale response that no longer
                            # matches the latest completed office turn, or when
                            # a newer office turn has already started.
                            if (
                                (
                                    latest_completed_office_turn_id
                                    and response_turn
                                    < latest_completed_office_turn_id
                                )
                                or (
                                    latest_started_office_turn_id
                                    > response_turn
                                    and not patient_playback_active
                                )
                            ) and not patient_playback_active:
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "Blocked STALE RESPONSE START "
                                        f"turn={response_turn} "
                                        f"after OFFICE TURN "
                                        f"started={latest_started_office_turn_id} "
                                        f"completed={latest_completed_office_turn_id}."
                                    ),
                                )
                                if delta_response_id:
                                    blocked_response_ids.add(
                                        delta_response_id
                                    )
                                await cancel_openai_response(
                                    openai_ws,
                                    "stale-delta-blocked",
                                )
                                continue

                            mulaw_8k = base64.b64decode(delta_b64)
                            now = elapsed()

                            if not patient_generation_active:
                                patient_generation_active = True
                                patient_playback_active = True
                                patient_output_started_at = now
                                reset_patient_turn_stats()
                                active_audio_turn_id = response_turn
                                if response_created_turn_id is None:
                                    response_created_turn_id = response_turn
                                if (
                                    delta_response_id
                                    and active_openai_response_id is None
                                ):
                                    active_openai_response_id = (
                                        delta_response_id
                                    )
                                cancel_response_watchdog()

                                latency_note = ""
                                if office_last_turn_end_at is not None:
                                    latency = (
                                        patient_output_started_at
                                        - office_last_turn_end_at
                                    )
                                    latency_note = (
                                        " Response start latency: "
                                        f"{latency:.1f}s."
                                    )

                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        f"RESPONSE START turn="
                                        f"{active_audio_turn_id} at "
                                        f"{patient_output_started_at:.1f}s."
                                        + latency_note
                                    ),
                                )
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        f"FIRST AUDIO turn={active_audio_turn_id} "
                                        f"response_id={active_openai_response_id}."
                                    ),
                                )
                                log_turn_state("response-start")

                            if outbound_last_delta_at is not None:
                                gap_ms = (
                                    now - outbound_last_delta_at
                                ) * 1000.0
                                if gap_ms > outbound_max_delta_gap_ms:
                                    outbound_max_delta_gap_ms = gap_ms
                            outbound_last_delta_at = now

                            outbound_openai_bytes += len(mulaw_8k)
                            openai_audio_deltas += 1
                            await send_twilio_audio(mulaw_8k)

                        elif event in {
                            "response.output_audio_transcript.delta",
                            "response.audio_transcript.delta",
                        }:
                            text = message.get("delta")
                            if text:
                                if office_buffer:
                                    append_transcript(
                                        transcript_path,
                                        "Office",
                                        office_buffer,
                                    )
                                    office_buffer.clear()
                                patient_buffer.append(text)

                        elif event in {
                            "conversation.item.input_audio_transcription.delta",
                        }:
                            pass

                        elif event in {
                            "conversation.item.input_audio_transcription.completed",
                        }:
                            text = message.get("transcript")
                            if text:
                                office_buffer.clear()
                                office_buffer.append(text)
                                append_transcript(
                                    transcript_path,
                                    "Office",
                                    office_buffer,
                                )
                                office_buffer.clear()

                        elif event in {
                            "response.output_audio.done",
                            "response.audio.done",
                        }:
                            if patient_buffer:
                                append_transcript(
                                    transcript_path,
                                    "Patient",
                                    patient_buffer,
                                )
                                patient_buffer.clear()

                        elif event == "response.done":
                            response_obj = message.get("response") or {}
                            response_id = (
                                response_obj.get("id")
                                or active_openai_response_id
                            )
                            status = response_obj.get("status")
                            done_turn = (
                                active_audio_turn_id
                                or response_created_turn_id
                                or response_requested_turn_id
                            )
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    f"OpenAI response.done "
                                    f"turn={done_turn} "
                                    f"response_id={response_id} "
                                    f"status={status} "
                                    f"at {elapsed():.1f}s "
                                    f"(deltas={openai_audio_deltas})."
                                ),
                            )

                            if patient_generation_active:
                                output_end = elapsed()
                                duration = (
                                    output_end - patient_output_started_at
                                    if patient_output_started_at is not None
                                    else 0.0
                                )
                                write_diagnostic(
                                    diagnostics_path,
                                    (
                                        "OpenAI response.done audio flush at "
                                        f"{output_end:.1f}s "
                                        f"(turn={duration:.1f}s, "
                                        f"deltas={openai_audio_deltas})."
                                    ),
                                )
                                await finish_patient_turn_and_mark()
                            else:
                                # Completed (or cancelled/failed) with no audio.
                                # Intentional silence is a valid WAIT result.
                                cancel_response_watchdog()
                                if done_turn is not None:
                                    if status == "completed":
                                        intentional_silent_turns.add(
                                            done_turn
                                        )
                                        last_completed_response_turn_id = (
                                            done_turn
                                        )
                                        write_diagnostic(
                                            diagnostics_path,
                                            (
                                                "INTENTIONAL SILENCE / "
                                                "NO PATIENT RESPONSE "
                                                f"turn={done_turn} "
                                                f"response_id={response_id} "
                                                "(response.done completed with "
                                                "zero audio; waiting for next "
                                                "office turn)."
                                            ),
                                        )
                                    else:
                                        write_diagnostic(
                                            diagnostics_path,
                                            (
                                                f"RESPONSE DONE turn={done_turn} "
                                                f"response_id={response_id} "
                                                f"(no audio; status={status})."
                                            ),
                                        )
                                clear_response_request_state(done_turn)
                                log_turn_state("response-done-no-audio")

                            if patient_buffer:
                                append_transcript(
                                    transcript_path,
                                    "Patient",
                                    patient_buffer,
                                )
                                patient_buffer.clear()

                        elif event == "response.cancelled":
                            response_obj = message.get("response") or {}
                            response_id = (
                                response_obj.get("id")
                                or active_openai_response_id
                            )
                            turn_for_cancel = (
                                active_audio_turn_id
                                or response_created_turn_id
                                or response_requested_turn_id
                            )
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    f"OpenAI event: response.cancelled "
                                    f"turn={turn_for_cancel} "
                                    f"response_id={response_id}."
                                ),
                            )
                            cancel_response_watchdog()
                            clear_response_request_state(turn_for_cancel)
                            log_turn_state("openai-response-cancelled")

                        elif event == "error":
                            write_diagnostic(
                                diagnostics_path,
                                (
                                    "OPENAI ERROR: "
                                    + json.dumps(
                                        message,
                                        default=str,
                                    )
                                ),
                            )

                        elif event in {
                            "input_audio_buffer.cleared",
                            "input_audio_buffer.speech_started",
                            "input_audio_buffer.speech_stopped",
                            "response.output_item.added",
                            "response.content_part.added",
                            "response.content_part.done",
                            "response.output_item.done",
                            "rate_limits.updated",
                            "conversation.item.added",
                            "conversation.item.done",
                        }:
                            pass

                        else:
                            if event and not event.startswith(
                                "response.output_audio"
                            ):
                                write_diagnostic(
                                    diagnostics_path,
                                    f"OpenAI event: {event}",
                                )

                except asyncio.CancelledError:
                    write_diagnostic(
                        diagnostics_path,
                        "OpenAI receive task cancelled.",
                    )
                    raise

                except Exception as error:
                    write_diagnostic(
                        diagnostics_path,
                        (
                            "OPENAI RECEIVE ERROR at "
                            f"{elapsed():.1f}s: "
                            f"{type(error).__name__}: {error}"
                        ),
                    )
                    write_diagnostic(
                        diagnostics_path,
                        traceback.format_exc(),
                    )
                    await flush_remaining()
                    return "openai_receive_error"

            twilio_task = asyncio.create_task(
                receive_twilio(),
                name="twilio-receive",
            )
            openai_task = asyncio.create_task(
                receive_openai(),
                name="openai-receive",
            )

            done, pending = await asyncio.wait(
                {
                    twilio_task,
                    openai_task,
                },
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    result = task.result()
                    write_diagnostic(
                        diagnostics_path,
                        (
                            "TASK ENDED FIRST: "
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
        cancel_response_watchdog()
        write_diagnostic(
            diagnostics_path,
            (
                "Call session closed. "
                f"Elapsed={elapsed():.1f}s, "
                f"Twilio chunks={twilio_media_chunks}, "
                f"OpenAI audio deltas={openai_audio_deltas}."
            ),
        )
        print("Call session closed cleanly.", flush=True)
