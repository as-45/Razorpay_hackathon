import os, razorpay
from dotenv import load_dotenv

load_dotenv()

client = razorpay.Client(auth=(os.getenv("RZP_KEY_ID"),
                               os.getenv("RZP_KEY_SECRET")))

order = client.order.create({
    "amount": 164000,
    "currency": "INR",
    "receipt": "trace_001",
    "notes": {"mandate_id": "mnd_001"},
})
print("ORDER:", order["id"], order["status"], order["amount"])