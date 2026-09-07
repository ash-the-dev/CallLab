import asyncio
import os
import wave
import truststore

truststore.inject_into_ssl()

from google import genai
from config import GEMINI_API_KEY

MODEL = "gemini-3.1-flash-live-preview"


async def main():
    client = genai.Client(api_key=GEMINI_API_KEY)

    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": "Kore"
                }
            }
        },
    }

    audio_data = bytearray()

    print("Connecting...")

    async with client.aio.live.connect(
        model=MODEL,
        config=config,
    ) as session:

        print("Connected.")

        await session.send_realtime_input(
            text=(
                "You are a patient calling a doctor's office. "
                "Say hello and naturally ask to schedule an "
                "appointment for next Tuesday."
            )
        )

        print("Waiting for speech...")

        async for response in session.receive():

            if response.server_content and response.server_content.model_turn:
                for part in response.server_content.model_turn.parts:

                    if part.inline_data and part.inline_data.data:
                        # Ignore tiny metadata/non-audio payloads.
                        if len(part.inline_data.data) > 100:
                            audio_data.extend(part.inline_data.data)

            if (
                response.server_content
                and response.server_content.turn_complete
            ):
                break

    os.makedirs("calls", exist_ok=True)

    output_path = "calls/test-live.wav"

    with wave.open(output_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(audio_data)

    print(f"Saved {len(audio_data)} audio bytes to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())