import asyncio
import queue
import wave
import truststore
import sounddevice as sd
import numpy as np

truststore.inject_into_ssl()

from google import genai
from google.genai import types
from config import GEMINI_API_KEY

MODEL = "gemini-3.1-flash-live-preview"

INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHANNELS = 1
BLOCKSIZE = 1600  # 100 ms at 16 kHz

audio_queue = queue.Queue()


def mic_callback(indata, frames, time, status):
    if status:
        print("Mic status:", status)

    pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
    audio_queue.put(pcm)


async def send_microphone(session):
    print("Listening. Speak into your microphone...")

    with sd.InputStream(
        samplerate=INPUT_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=BLOCKSIZE,
        callback=mic_callback,
    ):
        for _ in range(100):  # about 10 seconds
            chunk = await asyncio.to_thread(audio_queue.get)

            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type="audio/pcm;rate=16000",
                )
            )

    await session.send_realtime_input(audio_stream_end=True)
    print("Mic capture finished.")


async def main():
    client = genai.Client(api_key=GEMINI_API_KEY)

    config = {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": {},
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": "Kore"
                }
            }
        },
        "system_instruction": (
            "You are a realistic patient calling a doctor's office. "
            "Respond naturally and briefly to what you hear."
        ),
    }

    response_audio = bytearray()

    async with client.aio.live.connect(
        model=MODEL,
        config=config,
    ) as session:

        await send_microphone(session)

        print("Waiting for Gemini response...")

        async for response in session.receive():

            if (
                response.server_content
                and response.server_content.input_transcription
            ):
                text = response.server_content.input_transcription.text
                if text:
                    print("Heard:", text)

            if (
                response.server_content
                and response.server_content.model_turn
            ):
                for part in response.server_content.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        if len(part.inline_data.data) > 100:
                            response_audio.extend(part.inline_data.data)

            if (
                response.server_content
                and response.server_content.turn_complete
            ):
                break

    with wave.open("calls/two-way-response.wav", "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OUTPUT_RATE)
        wav.writeframes(response_audio)

    print("Saved Gemini response to calls/two-way-response.wav")


if __name__ == "__main__":
    asyncio.run(main())