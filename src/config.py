import os
from dotenv import load_dotenv

load_dotenv()

AI_VOICE_PROVIDER = (
    os.getenv("AI_VOICE_PROVIDER", "openai").strip().lower()
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL = os.getenv(
    "OPENAI_REALTIME_MODEL",
    "gpt-realtime-2.1",
)
OPENAI_REALTIME_VOICE = os.getenv(
    "OPENAI_REALTIME_VOICE",
    "sage",
)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PGAI_TEST_NUMBER = os.getenv("PGAI_TEST_NUMBER")
SELF_TEST_NUMBER = os.getenv("SELF_TEST_NUMBER")

if AI_VOICE_PROVIDER not in {"openai", "gemini"}:
    raise RuntimeError(
        "AI_VOICE_PROVIDER must be 'openai' or 'gemini'"
    )

if AI_VOICE_PROVIDER == "openai":
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY in .env")
elif AI_VOICE_PROVIDER == "gemini":
    if not GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY in .env")

# Required for any Twilio call placement.
if not TWILIO_ACCOUNT_SID:
    raise RuntimeError("Missing TWILIO_ACCOUNT_SID in .env")

if not TWILIO_AUTH_TOKEN:
    raise RuntimeError("Missing TWILIO_AUTH_TOKEN in .env")

if not TWILIO_PHONE_NUMBER:
    raise RuntimeError("Missing TWILIO_PHONE_NUMBER in .env")

# PGAI_TEST_NUMBER and SELF_TEST_NUMBER are mode-specific.
# run.py validates the destination for the selected mode.
