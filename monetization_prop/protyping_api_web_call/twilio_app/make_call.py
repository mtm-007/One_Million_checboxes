# Download the helper library from https://www.twilio.com/docs/python/install
import os
from dotenv import load_dotenv

load_dotenv()
from twilio.rest import Client

# Find your Account SID and Auth Token at twilio.com/console
# and set the environment variables. See http://twil.io/secure

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(account_sid, auth_token)

call = client.calls.create(
    url="http://demo.twilio.com/docs/voice.xml",
    to="+15109994767",
    from_="+18773679205",
)

print(call.sid)