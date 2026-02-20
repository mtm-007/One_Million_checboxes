from flask import Flask, render_template, request, jsonify
from vonage import Auth, Vonage
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Vonage credentials
VONAGE_API_KEY = os.environ.get('VONAGE_API_KEY')
VONAGE_API_SECRET = os.environ.get('VONAGE_API_SECRET')
VONAGE_APPLICATION_ID = os.environ.get('VONAGE_APPLICATION_ID')
VONAGE_PRIVATE_KEY_PATH = os.environ.get('VONAGE_PRIVATE_KEY_PATH', 'private.key')
VONAGE_PHONE_NUMBER = os.environ.get('VONAGE_PHONE_NUMBER')
YOUR_PHONE_NUMBER = os.environ.get('YOUR_PHONE_NUMBER')

# Debug: Print credentials (remove in production)
print(f"Vonage API Key: {VONAGE_API_KEY}")
print(f"Vonage Application ID: {VONAGE_APPLICATION_ID}")
print(f"Vonage Phone Number: {VONAGE_PHONE_NUMBER}")
print(f"Private Key Path: {VONAGE_PRIVATE_KEY_PATH}")

# Read private key
try:
    with open(VONAGE_PRIVATE_KEY_PATH, 'rb') as f:
        private_key = f.read()
    print("✅ Private key loaded successfully")
except FileNotFoundError:
    print(f"❌ Private key file not found at: {VONAGE_PRIVATE_KEY_PATH}")
    private_key = None

# Initialize Vonage client (SDK v4+) with JWT authentication
if VONAGE_APPLICATION_ID and private_key:
    vonage_client = Vonage(Auth(application_id=VONAGE_APPLICATION_ID, private_key=private_key))
    print("✅ Vonage client initialized with JWT authentication")
else:
    print("❌ Cannot initialize Vonage client - missing Application ID or Private Key")
    vonage_client = None

def format_phone_number(number, default_country_code='1'):
    """
    Format phone number to E.164 without + sign (Vonage v4 requirement)
    Examples:
        5109994767 -> 15109994767
        +15109994767 -> 15109994767
        15109994767 -> 15109994767
    """
    if not number:
        return None
    
    # Remove all non-digit characters
    clean_number = ''.join(filter(str.isdigit, number))
    
    # If number doesn't start with country code, add it
    if len(clean_number) == 10:  # US/Canada number without country code
        clean_number = default_country_code + clean_number
    
    return clean_number

# Add Content Security Policy
@app.after_request
def set_csp(response):
    response.headers['Content-Security-Policy'] = (
        "script-src 'self' 'unsafe-inline' "
        "https://unpkg.com https://cdn.jsdelivr.net https://nexmo-client.s3.amazonaws.com; "
        "connect-src 'self' https://*.nexmo.com https://*.vonage.com wss://*.nexmo.com wss://*.vonage.com"
    )
    return response

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/make-call', methods=['POST'])
def make_call():
    """Initiate outbound call - supports both direct and bridge modes"""
    try:
        # Check if client is initialized
        if not vonage_client:
            return jsonify({
                'error': 'Vonage client not initialized',
                'message': 'Make sure VONAGE_APPLICATION_ID and private.key file are configured'
            }), 500
        
        data = request.json
        to_number = data.get('to_number')
        call_mode = data.get('mode', 'direct')
        
        if not to_number:
            return jsonify({'error': 'Phone number is required'}), 400
        
        # Validate credentials
        if not VONAGE_API_KEY or not VONAGE_API_SECRET:
            return jsonify({
                'error': 'Vonage API credentials not configured',
                'message': 'Check VONAGE_API_KEY and VONAGE_API_SECRET in .env file'
            }), 500
        
        if not VONAGE_PHONE_NUMBER:
            return jsonify({
                'error': 'VONAGE_PHONE_NUMBER not configured',
                'message': 'Add VONAGE_PHONE_NUMBER to your .env file (without + sign)'
            }), 500
        
        # Format numbers properly (adds country code if missing)
        to_number_clean = format_phone_number(to_number)
        from_number_clean = format_phone_number(VONAGE_PHONE_NUMBER)
        
        print(f"Making call - To: {to_number_clean}, From: {from_number_clean}")
        
        # IMPORTANT: Use ngrok URL, not localhost
        ngrok_url = os.environ.get('NGROK_URL', request.url_root.rstrip('/'))
        base_url = ngrok_url
        
        print(f"Using base URL: {base_url}")
        
        if call_mode == 'bridge':
            # Bridge mode: Call your phone first, then connect to target
            if not YOUR_PHONE_NUMBER:
                return jsonify({
                    'error': 'YOUR_PHONE_NUMBER not configured',
                    'message': 'Add YOUR_PHONE_NUMBER to .env for bridge mode'
                }), 500
            
            # Store target in session or database (simplified here)
            app.config['CURRENT_TARGET'] = to_number_clean
            
            your_number_clean = format_phone_number(YOUR_PHONE_NUMBER)
            
            print(f"Bridge call - Your phone: {your_number_clean}, Target: {to_number_clean}")
            
            # Call your phone first
            response = vonage_client.voice.create_call({
                'to': [{'type': 'phone', 'number': your_number_clean}],
                'from_': {'type': 'phone', 'number': from_number_clean},
                'answer_url': [f"{base_url}/webhooks/answer-bridge"]
            })
            
            return jsonify({
                'success': True,
                'call_uuid': response.uuid,
                'message': f'Bridge call initiated. Your phone will ring first.'
            })
        else:
            # Direct mode: Call target with automated message
            print(f"Direct call attempt - To: {to_number_clean}, From: {from_number_clean}")
            
            # Option 1: Use answer_url (requires webhook to be accessible)
            response = vonage_client.voice.create_call({
                'to': [{'type': 'phone', 'number': to_number_clean}],
                'from_': {'type': 'phone', 'number': from_number_clean},
                'answer_url': [f"{base_url}/webhooks/answer-direct"]
            })
            
            # Option 2: Use NCCO directly (uncomment to test without webhooks)
            # response = vonage_client.voice.create_call({
            #     'to': [{'type': 'phone', 'number': to_number_clean}],
            #     'from_': {'type': 'phone', 'number': from_number_clean},
            #     'ncco': [
            #         {
            #             "action": "talk",
            #             "text": "Hello! This is a test call from your Vonage application."
            #         }
            #     ]
            # })
            
            print(f"Call created successfully: {response}")
            
            return jsonify({
                'success': True,
                'call_uuid': response.uuid,
                'message': 'Direct call initiated successfully'
            })
    
    except Exception as e:
        print(f"Error making call: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/webhooks/answer-direct', methods=['GET', 'POST'])
def answer_direct():
    """Handle direct call - automated message"""
    print("=" * 50)
    print("🔔 ANSWER WEBHOOK CALLED!")
    print(f"Method: {request.method}")
    print(f"Request data: {request.values.to_dict()}")
    print(f"Headers: {dict(request.headers)}")
    print("=" * 50)
    
    ncco = [
        {
            "action": "talk",
            "text": "Hello! This is an automated call from your web calling application. Thank you for answering. Have a great day!"
        }
    ]
    
    print(f"Returning NCCO: {ncco}")
    return jsonify(ncco)

@app.route('/webhooks/answer-bridge', methods=['GET', 'POST'])
def answer_bridge():
    """Handle bridge call - connect to target"""
    target_number = app.config.get('CURRENT_TARGET')
    
    if target_number:
        from_clean = format_phone_number(VONAGE_PHONE_NUMBER)
        
        ncco = [
            {
                "action": "talk",
                "text": "Connecting your call now. Please wait."
            },
            {
                "action": "connect",
                "timeout": 30,
                "from": from_clean,
                "endpoint": [
                    {
                        "type": "phone",
                        "number": target_number
                    }
                ]
            }
        ]
    else:
        ncco = [
            {
                "action": "talk",
                "text": "Sorry, there was an error connecting your call."
            }
        ]
    
    return jsonify(ncco)

@app.route('/webhooks/answer-browser', methods=['GET', 'POST'])
def answer_browser():
    """Handle browser call - connect to phone number"""
    to_number = request.args.get('to') or request.values.get('to')
    
    if to_number:
        to_clean = format_phone_number(to_number)
        from_clean = format_phone_number(VONAGE_PHONE_NUMBER)
        
        ncco = [
            {
                "action": "talk",
                "text": f"Calling {to_number}. Please wait."
            },
            {
                "action": "connect",
                "timeout": 30,
                "from": from_clean,
                "endpoint": [
                    {
                        "type": "phone",
                        "number": to_clean
                    }
                ]
            }
        ]
    else:
        ncco = [
            {
                "action": "talk",
                "text": "No phone number provided. Please try again."
            }
        ]
    
    return jsonify(ncco)

@app.route('/webhooks/event', methods=['GET', 'POST'])
def event_webhook():
    """Handle call events"""
    data = request.values.to_dict() if request.method == 'GET' else request.json
    print(f"Call event: {data}")
    
    # Check for routing failures
    if data.get('status') == 'failed':
        print(f"⚠️ Call failed - Detail: {data.get('detail')}, SIP Code: {data.get('sip_code')}")
    
    return ('', 204)

@app.route('/token', methods=['GET'])
def get_token():
    """Generate Vonage Client SDK JWT token for browser calling"""
    try:
        # Check if application ID is configured
        app_id = os.environ.get('VONAGE_APPLICATION_ID')
        private_key_path = os.environ.get('VONAGE_PRIVATE_KEY_PATH', 'private.key')
        
        if not app_id:
            return jsonify({
                'error': 'Vonage Application not configured',
                'message': 'Create a Vonage Application and add VONAGE_APPLICATION_ID to .env'
            }), 500
        
        # Check if private key exists
        if not os.path.exists(private_key_path):
            return jsonify({
                'error': 'Private key file not found',
                'message': f'Place your private.key file in the app directory'
            }), 500
        
        # Read private key
        with open(private_key_path, 'rb') as key_file:
            private_key = key_file.read()
        
        # Generate JWT token
        import jwt
        import time
        
        payload = {
            'application_id': app_id,
            'iat': int(time.time()),
            'exp': int(time.time()) + 3600,  # 1 hour expiry
            'jti': f'jwt_{int(time.time())}',
            'sub': 'web_user'
        }
        
        token = jwt.encode(payload, private_key, algorithm='RS256')
        
        return jsonify({
            'token': token,
            'user_name': 'web_user'
        })
    
    except Exception as e:
        print(f"Token generation error: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)