import os, sys, razorpay
from dotenv import load_dotenv

load_dotenv()
client = razorpay.Client(auth=(os.getenv("RZP_KEY_ID"),
                               os.getenv("RZP_KEY_SECRET")))

link = client.payment_link.fetch(sys.argv[1])
print("STATUS      :", link["status"])
print("AMOUNT PAID :", link.get("amount_paid"))clear