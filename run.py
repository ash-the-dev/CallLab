from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from twilio.rest import Client

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from scenarios import (  # noqa: E402
    list_scenarios,
    load_scenario,
    resolve_scenario_path,
)

SERVER_PORT = 8000
STATE_PATH = ROOT / ".calllab_state.json"


def resolve_cloudflared() -> Path:
    """Locate cloudflared on PATH or common Windows install paths."""
    from shutil import which

    found = which("cloudflared")
    if found:
        return Path(found)

    candidates = [
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return path

    raise FileNotFoundError(
        "cloudflared was not found on PATH or in common install locations. "
        "Install it from https://developers.cloudflare.com/cloudflare-one/"
        "connections/connect-networks/downloads/ and ensure it is on PATH."
    )


load_dotenv(ROOT / ".env")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PGAI_TEST_NUMBER = os.getenv("PGAI_TEST_NUMBER")
SELF_TEST_NUMBER = os.getenv("SELF_TEST_NUMBER")


def parse_args():
    parser = argparse.ArgumentParser(
        description="CallLab autonomous patient simulator"
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Call your own phone instead of the PGAI assessment line.",
    )

    parser.add_argument(
        "--scenario",
        default=None,
        help=(
            "Manual scenario override (id, filename, or number). "
            "Examples: 4, 04, 04_medication_refill. "
            "Does not advance the automatic cycle pointer."
        ),
    )

    return parser.parse_args()


def read_cycle_state() -> dict:
    if not STATE_PATH.is_file():
        return {"next_scenario": 1}

    try:
        data = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {"next_scenario": 1}

    if not isinstance(data, dict):
        return {"next_scenario": 1}

    next_scenario = data.get("next_scenario", 1)
    try:
        next_scenario = int(next_scenario)
    except (TypeError, ValueError):
        next_scenario = 1

    return {"next_scenario": max(1, next_scenario)}


def write_cycle_state(next_scenario: int):
    payload = {"next_scenario": int(next_scenario)}
    STATE_PATH.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def select_scenario(
    manual_selector: str | None,
    *,
    advance_on_launch: bool,
):
    """
    Choose a scenario for this run.

    Returns:
      scenario_item, should_advance (bool)
    """
    scenarios = list_scenarios()
    if not scenarios:
        raise RuntimeError(
            "No numbered scenario YAML files found under scenarios/."
        )

    by_number = {
        item["number"]: item for item in scenarios
    }
    ordered_numbers = [item["number"] for item in scenarios]

    if manual_selector:
        path = resolve_scenario_path(manual_selector)
        scenario = load_scenario(path)
        item = {
            "number": None,
            "id": path.stem,
            "name": scenario.get("name") or path.stem,
            "path": path,
        }
        match = re.match(r"^(\d+)_", path.stem)
        if match:
            item["number"] = int(match.group(1))
        return item, False

    state = read_cycle_state()
    wanted = state["next_scenario"]

    if wanted not in by_number:
        # Recover safely if state points at a removed scenario.
        item = scenarios[0]
        write_cycle_state(item["number"])
        print(
            "[CallLab] Cycle state referenced missing scenario "
            f"{wanted}; recovering to "
            f"{item['id']}."
        )
    else:
        item = by_number[wanted]

    return item, advance_on_launch


def next_scenario_number(
    current_number: int,
    scenarios: list[dict],
) -> int:
    ordered = [item["number"] for item in scenarios]
    try:
        index = ordered.index(current_number)
    except ValueError:
        return ordered[0]
    return ordered[(index + 1) % len(ordered)]


def advance_cycle_pointer(current_item: dict):
    scenarios = list_scenarios()
    current_number = current_item.get("number")
    if current_number is None and scenarios:
        # Fall back by matching id.
        for item in scenarios:
            if item["id"] == current_item["id"]:
                current_number = item["number"]
                break

    if current_number is None:
        write_cycle_state(scenarios[0]["number"])
        return scenarios[0]["number"]

    nxt = next_scenario_number(current_number, scenarios)
    write_cycle_state(nxt)
    return nxt


def start_server(scenario_path: Path):
    print("\n[CallLab] Starting voice server...")
    print(f"[CallLab] Scenario: {scenario_path.as_posix()}")

    env = os.environ.copy()
    env["CALL_LAB_SCENARIO_PATH"] = str(scenario_path)

    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "src" / "server.py"),
        ],
        cwd=ROOT,
        env=env,
    )

    time.sleep(2)

    if process.poll() is not None:
        raise RuntimeError(
            "CallLab server failed to start."
        )

    print("[CallLab] Voice server ready.")

    return process


def start_tunnel():
    print("[CallLab] Starting secure tunnel...")

    cloudflared = resolve_cloudflared()

    process = subprocess.Popen(
        [
            str(cloudflared),
            "tunnel",
            "--url",
            f"http://localhost:{SERVER_PORT}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tunnel_url = None
    deadline = time.time() + 30

    while time.time() < deadline:
        line = process.stdout.readline()

        if not line:
            if process.poll() is not None:
                break

            time.sleep(0.1)
            continue

        match = re.search(
            r"https://[a-zA-Z0-9-]+\.trycloudflare\.com",
            line,
        )

        if match:
            tunnel_url = match.group(0)
            break

    if not tunnel_url:
        process.terminate()

        raise RuntimeError(
            "CallLab could not obtain a Cloudflare tunnel URL."
        )

    print("[CallLab] Tunnel ready.")

    # Continue consuming cloudflared output so its
    # stdout pipe cannot fill during a long call.
    def drain_output():
        if process.stdout:
            for _ in process.stdout:
                pass

    threading.Thread(
        target=drain_output,
        daemon=True,
    ).start()

    return process, tunnel_url


def place_call(
    tunnel_url,
    destination,
    mode,
):
    stream_url = (
        tunnel_url
        .replace("https://", "wss://")
        + "/media-stream"
    )

    print("\n[CallLab] Ready.")
    print(f"[CallLab] Mode: {mode}")
    print(f"[CallLab] Stream: {stream_url}")

    client = Client(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
    )

    call = client.calls.create(
        to=destination,
        from_=TWILIO_PHONE_NUMBER,
        record=True,
        twiml=f"""
<Response>
    <Connect>
        <Stream url="{stream_url}" />
    </Connect>
</Response>
""",
    )

    print("\n[CallLab] Call started.")
    print(f"[CallLab] Call SID: {call.sid}")
    print("[CallLab] Recording: enabled")

    return client, call.sid


def wait_for_call(
    client,
    call_sid,
):
    print(
        "[CallLab] Conversation running...\n"
    )

    terminal_statuses = {
        "completed",
        "busy",
        "failed",
        "no-answer",
        "canceled",
    }

    while True:
        call = (
            client
            .calls(call_sid)
            .fetch()
        )

        if call.status in terminal_statuses:
            print(
                f"\n[CallLab] Call finished: "
                f"{call.status}"
            )

            if call.duration:
                print(
                    f"[CallLab] Duration: "
                    f"{call.duration} seconds"
                )

            return

        time.sleep(2)


def stop_process(
    process,
    name,
):
    if not process:
        return

    if process.poll() is not None:
        return

    print(
        f"[CallLab] Stopping {name}..."
    )

    process.terminate()

    try:
        process.wait(
            timeout=5
        )

    except subprocess.TimeoutExpired:
        process.kill()


def validate_config(
    self_test,
):
    required = {
        "TWILIO_ACCOUNT_SID":
            TWILIO_ACCOUNT_SID,

        "TWILIO_AUTH_TOKEN":
            TWILIO_AUTH_TOKEN,

        "TWILIO_PHONE_NUMBER":
            TWILIO_PHONE_NUMBER,
    }

    if self_test:
        required[
            "SELF_TEST_NUMBER"
        ] = SELF_TEST_NUMBER

    else:
        required[
            "PGAI_TEST_NUMBER"
        ] = PGAI_TEST_NUMBER

    missing = [
        name
        for name, value
        in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing .env values: "
            + ", ".join(missing)
        )


def main():
    args = parse_args()

    server = None
    tunnel = None
    selected = None
    should_advance = False
    advanced = False

    print("=" * 44)
    print(" CallLab")
    print(" Autonomous Patient Simulator")
    print("=" * 44)

    try:
        validate_config(
            args.self_test
        )

        if args.self_test:
            destination = (
                SELF_TEST_NUMBER
            )

            mode = "SELF TEST"

            print(
                "\n[CallLab] SELF TEST MODE"
            )

            print(
                "[CallLab] PGAI will NOT be called."
            )

            # Self-test never advances the production cycle.
            selected, should_advance = select_scenario(
                args.scenario,
                advance_on_launch=False,
            )

        else:
            destination = (
                PGAI_TEST_NUMBER
            )

            mode = "PGAI ASSESSMENT"

            print(
                "\n[CallLab] PGAI ASSESSMENT MODE"
            )

            # Manual --scenario does not advance; automatic does.
            selected, should_advance = select_scenario(
                args.scenario,
                advance_on_launch=args.scenario is None,
            )

        scenarios = list_scenarios()
        total = len(scenarios)
        number = selected.get("number")
        if number is None:
            label = selected["id"]
        else:
            label = f"{number:02d}/{total:02d}"

        print(
            f"\nScenario {label}: {selected['name']}"
        )
        print(
            f"[CallLab] Scenario ID: {selected['id']}"
        )
        if args.scenario:
            print(
                "[CallLab] Manual scenario override "
                "(cycle pointer unchanged)."
            )
        elif args.self_test:
            print(
                "[CallLab] Self-test uses current scenario "
                "without advancing cycle pointer."
            )
        else:
            print(
                "[CallLab] Automatic cycle scenario "
                "(pointer advances after Call SID)."
            )

        scenario_path = selected["path"]

        server = start_server(scenario_path)

        tunnel, tunnel_url = (
            start_tunnel()
        )

        client, call_sid = (
            place_call(
                tunnel_url,
                destination,
                mode,
            )
        )

        if should_advance:
            nxt = advance_cycle_pointer(selected)
            advanced = True
            print(
                "[CallLab] Cycle pointer advanced. "
                f"Next automatic scenario: {nxt:02d}."
            )

        wait_for_call(
            client,
            call_sid,
        )

        # Give server.py time to flush final
        # transcript and diagnostic messages.
        time.sleep(2)

        print(
            "\n[CallLab] Transcript saved in calls/"
        )

        print(
            "[CallLab] Diagnostics saved in calls/"
        )

        print(
            "[CallLab] Twilio recording saved remotely."
        )

        print(
            "[CallLab] Run complete."
        )

    except KeyboardInterrupt:
        print(
            "\n[CallLab] Interrupted."
        )
        if advanced:
            print(
                "[CallLab] Cycle pointer already advanced "
                "after Call SID."
            )

    except Exception as error:
        print(
            f"\n[CallLab] ERROR: {error}"
        )
        if not advanced and should_advance:
            print(
                "[CallLab] Cycle pointer NOT advanced "
                "(call was not launched)."
            )

    finally:
        stop_process(
            tunnel,
            "tunnel",
        )

        stop_process(
            server,
            "server",
        )


if __name__ == "__main__":
    main()
