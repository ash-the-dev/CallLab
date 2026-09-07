"""
Smoke-test outbound Twilio dialing.

Uses SELF_TEST_NUMBER from .env. Does not hardcode a personal number.
This places a real phone call.
"""

import os
import sys

from dotenv import load_dotenv
from twilio.rest import Client

from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
)

load_dotenv()

YOUR_PHONE = os.getenv("SELF_TEST_NUMBER")
if not YOUR_PHONE:
    print(
        "Set SELF_TEST_NUMBER in .env before running this smoke test.",
        file=sys.stderr,
    )
    sys.exit(1)

client = Client(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
)

call = client.calls.create(
    to=YOUR_PHONE,
    from_=TWILIO_PHONE_NUMBER,
    twiml="""
        <Response>
            <Say>
                Hello. This is a Call Lab test.
                The outbound phone connection is working.
            </Say>
        </Response>
    """,
)

print("Call started.")
print("Call SID:", call.sid)
