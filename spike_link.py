import os, razorpay
from dotenv import load_dotenv
import uuid
load_dotenv()
client = razorpay.Client(auth=(os.getenv("RZP_KEY_ID"),
                               os.getenv("RZP_KEY_SECRET")))

link = client.payment_link.create({
    "amount": 164000,
    "currency": "INR",
    "description": "2 boxes kaju katli",
    "notify": {"sms": False, "email": False},
    "reference_id": f"trace_{uuid.uuid4().hex[:8]}",
})
print("LINK ID :", link["id"])
print("URL     :", link["short_url"])
print("STATUS  :", link["status"])