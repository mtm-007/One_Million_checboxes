
import os
import subprocess
import json
import re
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()
import stripe
import markdown
from fasthtml.common import *
from starlette.responses import RedirectResponse


#from replit import db, web
#from werkzeug.utils import secure_filename

#create fasthtml app
app, rt = fast_app(
    hdrs=(
        Script(src="https://js.stripe.com/v3/"),
    )
)

#specify your apps urls
#DOMAIN = "https://journalary-pamela-soundlessly.ngrok-free.dev"

DOMAIN = os.environ.get("DOMAIN", "https://journalary-pamela-soundlessly.ngrok-free.dev")
#app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
stripe.api_key = os.getenv("STRIPE_API_KEY")
webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

#----local db json 
DB_FILE = "local_db.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"content": {}, "orders":{}}

def save_db(db_data):
    with open(DB_FILE, "w") as f:
        json.dump(db_data,f, indent=4)

db=load_db()

#Database setup
def db_init():
    #if "content" not in db.keys():
    if "content" not in db:
        db["content"] = {}
    if "orders" not in db:  #.keys():
        db["orders"] = {}
    #create directories
    if not os.path.exists("static"):
        os.mkdir("static")
    if not os.path.exists("content"):
        os.mkdir("content")
    if not os.path.exists("templates"):
        os.mkdir("templates")



db_init()
save_db(db)

#email validation
def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def render_templates(template_name, **context):
    """Read and render HTML template with context variables"""
    template_path = os.path.join("templates", template_name)
    if not os.path.exists(template_path):
        return f"Template {template_name} not found"
    
    with open(template_path, "r") as f:
        template_content = f.read()

    for key, value in context.items():
        template_content = template_content.replace(f"{{{{key}}}}", str(value))

    return NotStr(template_content)

#homepage
@app.get("/")
def homepage():
    return render_templates("index.html")


@app.post("/upload")
def upload(email:str, prompt:str):
    email = email.strip()
    prompt = prompt.strip()

    if not email or not prompt:
        return render_templates("error.html", 
                                message="Email and prompt are required",
                                back_link="/")

    unique_id = str(uuid4())
    #add the unique_id and filename + email to the database
    db["content"][unique_id] = {
        "email": email, 
        "prompt":prompt, 
        "status": "pending"
    }#"path": file_path, 
    save_db(db)

    return RedirectResponse(url=f"/checkout/{unique_id}", status_code=303)


#checkout page
@app.get("/checkout/{file_id}")
def checkout(file_id:str):
    #check the file exists in the database
    file_info = db["content"].get(file_id)
    if not file_info:
        return render_templates("error.html", message ="Invalid file ID", back_link="/")
    
    #pull out relevant info
    email = file_info["email"]

    try:
        success_url=DOMAIN + '/success?session_id={CHECKOUT_SESSION_ID}'
        cancel_url=DOMAIN + '/cancel'

        print(f"Creating stripe session with:")
        print(f"   success_url: {success_url}")
        print(f"  cancel_url: {cancel_url}")

        #create stripe checkout session
        session = stripe.checkout.Session.create(
            payment_method_types = ["card"],
            line_items = [{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount' : 350,
                    'product_data': {
                        'name': 'AI Generated Image',
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=email,
            #add metadata to help track order
            metadata={'file_id': file_id}
        )
        print(f"Stripe session created: {session['id']}")
        print(f"Checkout URL: {session['url']}")

        #link session in file_id
        db["orders"][session["id"]]={ 
            "file_id": file_id,  
            "email":email, 
            "processed":False
        }
        save_db(db)

        #redirect to stripe checkout
        return RedirectResponse(url=session['url'], status_code=303)
    
    except stripe.error.StripeError as e:
        print(f"Stripe error: {e}")
        return render_templates("error.html",
                                message="Payment system error. Please try again.",
                                back_link="/")
    

#the page they'll see if payment is cancelled
#@app.route("/cancel")
@app.get("/cancel")
def cancel():
    return render_templates("cancel.html")


#@app.route("/success")
@app.get("/success")
def success(session_id: str = None):
    print("="*50)
    print("SUCCESS ROUTE CALLED!")
    print(f"session_id: {session_id}")
    print("="*50)

    if not session_id:
        return render_templates("error.html",
                                message="No session ID provided",
                                back_link="/")
    try:
        #get the session id from stripe directly
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != 'paid':
            return render_templates("error.html", 
                                    message="Payment not completed",
                                    back_link="/")

        #the session contains metadata or we can search our orders, but first
        # lets reload the DB in case webhook already processed
        global db
        db = load_db()
        print(f"Orders in DB: {list(db['orders'].keys())}")

        #try to find the order
        order = db["orders"].get(session_id)
        print(f"Found order: {order}")
        
        if order:
            #find the file_id associated with this session
            file_id = order.get("file_id") 
            print(f"File ID: {file_id}")
            if file_id:
                #order found, render success page with file tracking
                return render_templates("success.html", file_id=file_id)
        
        return render_templates("processing.html")
        
    except stripe.error.StripeError as e:
        print(f"Stripe error: {e}")
        return render_templates("error.html", 
                                message = "Error retrieving payment session",
                                back_link="/")


#@app.route("/check_status/<file_id>", methods=["GET"])
@app.get("/check_status/{file_id}")
def check_status(file_id):
    """API endpoint to check if image processing is complete"""
    global db
    db = load_db()

    content = db["content"].get(file_id)
    if not content:
        return {"status": "not_found"}, 404
    
    #check if we have an image url stored
    if content.get("image_url"):
        return {
            "status": "complete",
            "image_url": content["image_url"],
            "prompt": content["prompt"]
        }
    else:
        return {"status": "processing"}
    

#you could do the processeing of the image hee, but for
#ease of debugging its in a seperate script.
def process_image(email, prompt,file_id):#filename, 
    #get environment variable to pass to subprocess
    env = os.environ.copy()

    command =[
        "python", "processing_image.py",
        "--email",email, 
        "--prompt",prompt, 
        "--file_id", file_id, # "--file_path", filename,
    ]
    try:
        subprocess.Popen(command, env=env)
    except Exception as e:
        print(f"Error starting image processing: {e}")


#@stripe_webhook
@app.post("/webhook")#, methods=["POST"])
async def stripe_webhook(request):

    payload = await request.body()
    sig_header = request.headers.get('Stripe-Signature')

    #verify the stripe webhook signature
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret) 
        
    except ValueError as e:
        print(f"Invalid payload: {e}")
        return {'error': 'Invalid payload'}, 400
    except stripe.error.SignatureVerificationError as e:
        print(f"Invalid signature: {e}")
        return {'error': 'Invalid signature'}, 400

    print(f"Received webhook event: {event['type']}")

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        #reload db to get latest data
        global db
        db = load_db()

        order = db["orders"].get(session["id"])
        if not order:
            return {'status': 'order not found'}, 200
        
        if order.get("processed"):
            return {'status': 'already processed'}, 200
        
        file_id = order["file_id"]
        content = db["content"].get(file_id)

        if content:
            print(f"Starting image processing for file_id: {file_id}")
            process_image(content["email"], content["prompt"], file_id)#content["path"], 
            order["processed"] = True
            save_db(db) #when using local json db
            print(f"Order marked as processed: {session['id']}")
        return {'status': 'ok'}, 200
    # Add this return for other event types
    return {'status': 'event received'}, 200
        

@app.get("/readme")
def readme():
    if os.path.exists("README.md"):
        with open("README.md", "r") as f:
            md_content = markdown.markdown(f.read(), extensions=["fenced_code"])
            return NotStr(md_content)
    return P("README not found")


#run
if __name__=="__main__":
    print("Registered routes:")
    for rule in app.routes:
        print(f" {rule.path} - {getattr(rule, 'method', 'GET')}")
    print(f"\nStarting server at http://0.0.0.0:5000")
    serve(host="0.0.0.0", port=5000)