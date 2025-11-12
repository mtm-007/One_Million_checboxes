from flask import Flask, render_template, request, jsonify
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Twilio credentials (set these as environment variables)
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')

# Debug: Print to verify credentials are loaded (remove in production)
print(f"Account SID: {TWILIO_ACCOUNT_SID}")
print(f"Phone Number: {TWILIO_PHONE_NUMBER}")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/make-call', methods=['POST'])
def make_call():
    """Initiate an outbound call"""
    try:
        data = request.json
        to_number = data.get('to_number')
        
        if not to_number:
            return jsonify({'error': 'Phone number is required'}), 400
        
        # Validate credentials are loaded
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
            return jsonify({
                'error': 'Twilio credentials not configured. Check your .env file.',
                'debug': {
                    'has_sid': bool(TWILIO_ACCOUNT_SID),
                    'has_token': bool(TWILIO_AUTH_TOKEN),
                    'has_phone': bool(TWILIO_PHONE_NUMBER)
                }
            }), 500
        
        # Create a call
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=request.url_root + 'voice',
            status_callback=request.url_root + 'call-status',
            status_callback_event=['initiated', 'ringing', 'answered', 'completed']
        )
        
        return jsonify({
            'success': True,
            'call_sid': call.sid,
            'message': 'Call initiated successfully'
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/voice', methods=['POST'])
@app.route('/voice', methods=['POST'])
def voice():
    """Handle voice response for outbound calls"""
    response = VoiceResponse()
    
    gather = response.gather(num_digits=1, action='/handle-key', method='POST', timeout=10)
    gather.say('Hello! Press 1 to connect to a representative, or press 2 to leave a message.', voice='alice')
    
    # If no input
    response.say('We did not receive any input. Goodbye!', voice='alice')
    
    return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/handle-key', methods=['POST'])
def handle_key():
    """Handle keypress from caller"""
    digit = request.form.get('Digits')
    response = VoiceResponse()
    
    if digit == '1':
        response.say('Connecting you now.', voice='alice')
        dial = Dial()
        dial.number('+14699388764')  # Your phone
        response.append(dial)
    elif digit == '2':
        response.say('Please leave a message after the beep.', voice='alice')
        response.record(max_length=30, action='/handle-recording')
    else:
        response.say('Invalid option. Goodbye.', voice='alice')
    
    return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/handle-recording', methods=['POST'])
def handle_recording():
    """Handle voice recording"""
    recording_url = request.form.get('RecordingUrl')
    print(f"Recording available at: {recording_url}")
    
    response = VoiceResponse()
    response.say('Thank you for your message. Goodbye!', voice='alice')
    return str(response), 200, {'Content-Type': 'text/xml'}
@app.route('/incoming-call', methods=['POST'])
def incoming_call():
    """Handle incoming calls"""
    response = VoiceResponse()
    response.say('Welcome! You have reached the web calling application.', voice='alice')
    
    # Add a menu or dial to a client
    dial = Dial()
    dial.client('web_user')  # This connects to a Twilio Client
    response.append(dial)
    
    return str(response), 200, {'Content-Type': 'text/xml'}

@app.route("/call-status", methods=["GET", "POST"])
def call_status():
    from flask import request

    data = request.values.to_dict()
    print("📞 Call Status Callback:", data)

    return "OK", 200

@app.route('/token', methods=['GET'])
def token():
    """Generate Twilio Client token for browser calling"""
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant
    
    # Generate a random identity or use a user identifier
    identity = request.args.get('identity', 'web_user')
    
    # Create access token
    access_token = AccessToken(
        TWILIO_ACCOUNT_SID,
        os.environ.get('TWILIO_API_KEY'),
        os.environ.get('TWILIO_API_SECRET'),
        identity=identity
    )
    
    # Create a Voice grant and add to token
    voice_grant = VoiceGrant(
        outgoing_application_sid=os.environ.get('TWILIO_TWIML_APP_SID'),
        incoming_allow=True
    )
    access_token.add_grant(voice_grant)
    
    return jsonify({
        'identity': identity,
        'token': access_token.to_jwt()
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)