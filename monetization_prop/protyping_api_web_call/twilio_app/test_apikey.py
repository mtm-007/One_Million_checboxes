from dotenv import load_dotenv
import os

load_dotenv()  # loads .env from current folder

print("TWILIO_ACCOUNT_SID:", os.getenv("TWILIO_ACCOUNT_SID"))
print("TWILIO_API_KEY:", os.getenv("TWILIO_API_KEY"))
print("TWILIO_API_SECRET:", os.getenv("TWILIO_API_SECRET"))
print("TWIML_APP_SID:", os.getenv("TWIML_APP_SID"))
print("TWILIO_NUMBER:", os.getenv("TWILIO_NUMBER"))
