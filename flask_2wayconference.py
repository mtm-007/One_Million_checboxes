from flask import Flask, render_template, request, jsonify
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial
import os
from dotenv import load_dotenv
from flask_cors import CORS
app = Flask(__name__)
CORS(app, resources={r"/make-call": {"origins": "*"}, r"/token": {"origins": "*"}})


# Load environment variables from .env file
load_dotenv()

#app = Flask(__name__)

# Twilio credentials (set these as environment variables)
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')

# Debug: Print to verify credentials are loaded (remove in production)
print(f"Account SID: {TWILIO_ACCOUNT_SID}")
print(f"Phone Number: {TWILIO_PHONE_NUMBER}")
print(f"API Key: {os.environ.get('TWILIO_API_KEY')}")
print(f"API Secret: {os.environ.get('TWILIO_API_SECRET')}")
print(f"Browser Calling App SID: {os.environ.get('TWILIO_BROWSER_CALLING_APP_SID')}")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@app.route('/')
def index():
    return render_template('index.html')
@app.after_request
def set_csp(response):
    # Update the policy to allow inline scripts
    response.headers['Content-Security-Policy'] = "script-src 'self' 'unsafe-inline' https://media.twiliocdn.com https://sdk.twilio.com;"
    # Consider adding other directives like `default-src` as needed for your app
    return response

@app.route('/make-call', methods=['POST'])
def make_call():
    """Initiate an outbound call - Bridge mode"""
    try:
        data = request.json
        to_number = data.get('to_number')
        call_mode = data.get('mode', 'direct')  # 'direct' or 'bridge'
        
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
        
        if call_mode == 'bridge':
            # Bridge mode: Call your phone first, then connect to target
            your_phone = os.environ.get('YOUR_PHONE_NUMBER')
            
            if not your_phone:
                return jsonify({
                    'error': 'YOUR_PHONE_NUMBER not configured in .env file',
                    'message': 'Add YOUR_PHONE_NUMBER=+1234567890 to your .env file for bridge mode'
                }), 500
            
            # Store the target number for the voice endpoint to use
            app.config['CURRENT_TARGET'] = to_number
            
            # Create a call to YOUR phone first
            call = client.calls.create(
                to=your_phone,
                from_=TWILIO_PHONE_NUMBER,
                url=request.url_root + 'voice',
                status_callback=request.url_root + 'call-status',
                status_callback_event=['initiated', 'ringing', 'answered', 'completed']
            )
            
            return jsonify({
                'success': True,
                'call_sid': call.sid,
                'message': f'Bridge mode: Calling your phone first, then connecting to {to_number}'
            })
        else:
            # Direct mode: Call target directly with automated message
            call = client.calls.create(
                to=to_number,
                from_=TWILIO_PHONE_NUMBER,
                url=request.url_root + 'voice-direct',
                status_callback=request.url_root + 'call-status',
                status_callback_event=['initiated', 'ringing', 'answered', 'completed']
            )
            
            return jsonify({
                'success': True,
                'call_sid': call.sid,
                'message': 'Direct call initiated successfully'
            })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/voice-direct', methods=['POST'])
def voice_direct():
    """Handle voice response for direct calls (automated message)"""
    response = VoiceResponse()
    
    # Play automated message
    response.say('Hello! This is an automated call from your web calling application.', voice='alice')
    response.pause(length=1)
    response.say('Thank you for answering. This is a test call. Have a great day!', voice='alice')
    
    return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/voice', methods=['POST'])
def voice():
    """Handle voice response for outbound calls"""
    response = VoiceResponse()
    
    # Get the target number from config
    target_number = app.config.get('CURRENT_TARGET')
    
    if target_number:
        response.say('Connecting your call now. Please wait.', voice='alice')
        
        # Dial the target number
        dial = Dial(timeout=30, callerId=TWILIO_PHONE_NUMBER)
        dial.number(target_number)
        response.append(dial)
        
        response.say('The call has ended. Goodbye.', voice='alice')
    else:
        response.say('Hello! This is a call from your web calling application.', voice='alice')
    
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
    
    # Check if API key and secret are configured
    api_key = os.environ.get('TWILIO_API_KEY')
    api_secret = os.environ.get('TWILIO_API_SECRET')
    twiml_app_sid = os.environ.get('TWILIO_BROWSER_CALLING_APP_SID')
    
    print(f"Token endpoint - API Key: {api_key}")
    print(f"Token endpoint - API Secret: {'***' if api_secret else None}")
    print(f"Token endpoint - TwiML App SID: {twiml_app_sid}")
    
    if not api_key or not api_secret:
        error_msg = 'API Key and Secret not configured'
        print(f"ERROR: {error_msg}")
        return jsonify({
            'error': error_msg,
            'message': 'Please create API credentials in Twilio Console',
            'debug': {
                'has_api_key': bool(api_key),
                'has_api_secret': bool(api_secret),
                'has_twiml_app': bool(twiml_app_sid)
            }
        }), 500
    
    if not twiml_app_sid:
        error_msg = 'TwiML App SID not configured'
        print(f"ERROR: {error_msg}")
        return jsonify({
            'error': error_msg,
            'message': 'Please add TWILIO_BROWSER_CALLING_APP_SID to .env file'
        }), 500
    
    try:
        # Create access token
        access_token = AccessToken(
            TWILIO_ACCOUNT_SID,
            api_key,
            api_secret,
            identity=identity
        )
        
        # Create a Voice grant and add to token
        voice_grant = VoiceGrant(
            outgoing_application_sid=twiml_app_sid,
            incoming_allow=True
        )
        access_token.add_grant(voice_grant)
        
        print(f"Token generated successfully for identity: {identity}")
        
        return jsonify({
            'identity': identity,
            'token': access_token.to_jwt()
        })
    except Exception as e:
        print(f"ERROR generating token: {str(e)}")
        return jsonify({
            'error': f'Failed to generate token: {str(e)}'
        }), 500

@app.route('/voice-client', methods=['POST'])
def voice_client():
    """Handle voice calls from browser client"""
    response = VoiceResponse()
    
    # Get the phone number from the client
    to_number = request.form.get('To', request.values.get('To'))
    
    if to_number:
        response.say(f'Calling {to_number}. Please wait.', voice='alice')
        dial = Dial(callerId=TWILIO_PHONE_NUMBER)
        dial.number(to_number)
        response.append(dial)
    else:
        response.say('No phone number provided. Please try again.', voice='alice')
    
    return str(response), 200, {'Content-Type': 'text/xml'}

if __name__ == '__main__':
    app.run(debug=True, port=5000)