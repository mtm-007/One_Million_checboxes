import os
import subprocess
from uuid import uuid4


import markdown.extensions.fenced_code
import stripe 
from flask import(
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    url_for
)
from replit import db, web
from werkzeug.utils import secure_filename

#create flask app
app = Flask(__name__, static_folder='static', static_url_path='')

#specify your apps urls
DOMAIN = ""

app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
stripe.api_key = os.environ["STRIPE_API_KEY"]
webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]


#Database setup
def db_init():
    if "content" not in db.keys():
        db["content"] = {}
    if "orders" not in db.keys():
        db["orders"] = {}
    #create directories
    if not os.path.exists("static"):
        os.mkdir("static")
    if not os.path.exists("content"):
        os.mkdir("content")


db_init()


#homepage
@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

#when form submitted
@app.route("upload", methods=["POST"])
def upload():
    file = request.files['file']
    email = request.form['email']
    if file.filename == '':
        flash('No file selected for uploading')
        return redirect(url_for('home'))
    
    if file:
        filename = secure_filename(file.filename)
        unique_id = str(uuid4())
        file_path = os.path.join('content', unique_id + '_' + filename)
        file.save(file_path)

        #add the unique_id and filename + email to the database
        db["content"][unique_id] = {"path": file_path, "email": email}
        return redirect(url_for('checkout', file_id = unique_id))
    
    return "File upload failed"


#checkout page
@app.route("/checkout/<file_id>", methods=["GET"])
def checkout(file_id):

    #check the file exists in the database
    if file_id not in db["content"].keys():
        return "Invalid file ID"
    
    #pull out relevant info
    file_info = db["content"][file_id]
    email = file_info["email"]

    #create stripe checkout session
    session = stripe.checkout.Session.create(
        payment_method_types = ["card"],
        line_items = [{
            'price_data': {
                'currency': 'usd',
                'unit_amount' : 35,
                'product_data': {
                    'name': 'Your Image',
                },
            },
            'quantity': 1,
        }],
        mode='payment',
        success_url=DOMAIN+ '/success?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=DOMAIN+ '/cancel',
    )

    #link session in file_id
    db["orders"][session["id"]]={"file_id": file_id, "email":email}

    #redirect to stripe checkout
    return redirect(session['url'])


#the page they'll see if payment is cancelled
@app.route("/cancel")
def cancel():
    return render_template_string(
        "<h1>Cancelled</h1><p>Your payment was cancelled.</p><p><a href='/'>Go back to the homepage</a><p/>"
    )


#you could do the processeing of the image hee, but for
#ease of debugging its in a seperate script.
def process_image(filename, email):
    command =[
        "python", "processing_image.py", "--file_path", filename,"--email",email
    ]
    subprocess.Popen(command)


#stripe webhook
@app.route('/webhook', methods='POST')
def stripe_webhook():
    payload = request.get_data(as_text=True)
    signature = request.headers.get('Stripe-Signature')

    #verify the stripe webhook signature
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret) 
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    #handle the event
    if event['type']=='checkout.session.completed':
        session = event['data']['object']
        if session['id'] in db["orders"]:
            #retrive associated file_id from database
            file_id= db["orders"][session]['id']['file_id']

            #use this to fetch the associated file path and email
            file_path = db["content"][file_id]['path']
            email = db["content"][file_id]['email']

            #process the image (if it exists)
            if file_path:
                process_image(file_path, email)
        return jsonify({'status': 'success'}),200


#show readme
@app.route('/readme')
def readme():
    readme_file = open("README.md", "r")
    md_template_string = markdown.markdown(readme_file.read(), extensions=["fenced_code"])

    return md_template_string


#run
web.run(app)