import smtplib,ssl, os, argparse,requests
import base64
import json
import replicate
from replicate import Client
#import Sendinblue
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()

#replicate setup,with upscaling model for demo 
#replicate = replicate.Client(api_token=os.environ["REPLICATE_API_TOKEN"])
#REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
replicate_url = "mtm-007/custm_diffusion:4536d1407dddcdc365ecaee695283e2b1d3307bb214550c8e35d245abada3994"

#email setup
#sendinBlue api configuration
configuration = sib_api_v3_sdk.Configuration()

#initialize the SendinBlue API instance
API_V3_KEY = os.getenv("SIB_API_V3_KEY")
configuration.api_key['api-key'] = API_V3_KEY

api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

#function to send the image to the user
def send_email(subject, html, to_address=None, image_url=None):

    #Download the image
    image_data = requests.get(image_url).content

    #Encode the image in Base64
    image_base64 = base64.b64encode(image_data).decode('utf-8')

    #create a SendSmtEmailAttachement object
    attachment=sib_api_v3_sdk.SendSmtpEmailAttachment(
        content=image_base64, name="processes_image.jpg")
    
    #SendinBlue mailing parameters
    subject = subject
    html_content = html

    sender = {"name": "Why not Try Stripe", 
              "email": "gptagent.unlock@gmail.com"}           #Company name and Email


    #create a sendsmtp Email object with attachment
    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{
            "email":to_address,
            "name": to_address.split("@")[0]
        }],
        html_content=html_content,
        sender=sender,
        subject=subject,
        attachment=[attachment] #attach the image here
    )

    try:
        #send the email
        api_response = api_instance.send_transac_email(send_smtp_email)
        print(api_response)
        return {"message": "Email sent succesfully!"}
    except ApiException as e:
        print("Exception when calling SMTPApi->send_transac_email: %s\n" % e)

#replicate = Client(api_token=os.getenv("REPLICATE_API_TOKEN"))
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
print(f"DEBUG: REPLICATE_API_TOKEN loaded: {REPLICATE_API_TOKEN[:10] if REPLICATE_API_TOKEN else 'NOT FOUND'}...")

#the important bit 
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    #parser.add_argument('--file_path', type=str, required=True)
    parser.add_argument('--email', type=str, required=True)
    parser.add_argument('--prompt', type=str, required=True)
    parser.add_argument('--file_id', type=str, required=True)
    #parser.add_argument('--debug', type=bool, default=False)
    args = parser.parse_args()

    print(f"Generating image for prompt: {args.prompt}")

    output_url = replicate.run(
        replicate_url, 
        input={"prompt": args.prompt})#, "image": image })
    #if args.debug: print(output_url)
    output_url = output_url[0] if isinstance(output_url,list) else output_url
    print(f"Image Generated: {output_url}")

    DB_FILE = "local_db.json"

    with open(DB_FILE, "r")as f:
        db_data = json.load(f)

    if args.file_id in db_data["content"]:
        db_data["content"][args.file_id]["image_url"] = output_url

    with open(DB_FILE, "w") as f:
        json.dump(db_data, f, indent=4)

    #process the image and other inputs with replicate
    #with open(args.file_path, 'rb') as image:

    subject = "Your AI Generated Image"
    html_content = f"<p>Here is your AI-generated image based on your prompt:</p><p><em>\"{args.prompt}\"</em></p>"

    #send the result via email
    send_email(subject, html_content, args.email, output_url)
    print("Email sent successfully!")

    #and finally, delete the photo and any other user data
    #os.remove(args.file_path)