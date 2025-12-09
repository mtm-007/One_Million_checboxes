from flask import Flask, request, jsonify, Response, render_template
from flask_cors import CORS
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse
from flask import send_from_directory

import os
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_API_KEY = os.getenv("TWILIO_API_KEY")
TWILIO_API_SECRET = os.getenv("TWILIO_API_SECRET")
TWIML_APP_SID = os.getenv("TWIML_APP_SID")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

@app.route("/")
def index():
     return send_from_directory(".", "index.html")

@app.route("/token")
def generate_token():
    identity = request.args.get("identity", "webUser")

    voice_grant = VoiceGrant(
        outgoing_application_sid=TWIML_APP_SID,
        incoming_allow=False
    )

    access_token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_API_KEY,
        TWILIO_API_SECRET,
        identity=identity
    )
    access_token.add_grant(voice_grant)

    return jsonify(identity=identity, token=access_token.to_jwt())

@app.route("/voice", methods=["POST"])
def voice():
    to_number = request.form.get("To")
    twiml = VoiceResponse()

    if to_number and to_number.startswith("+"):
        twiml.dial(to_number, callerId=TWILIO_NUMBER)
    else:
        twiml.dial(client="webUser")

    return Response(str(twiml), mimetype="text/xml")

if __name__ == "__main__":
    app.run(port=3000, debug=True)
