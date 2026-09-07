import truststore
truststore.inject_into_ssl()

from google import genai
from config import GEMINI_API_KEY

client = genai.Client(api_key=GEMINI_API_KEY)

interaction = client.interactions.create(
    model="gemini-3.8-flash",
    input="You are a patient calling a doctor's office. In one sentence, ask to schedule an appointment for next Tuesday.",
)

print(interaction.output_text)