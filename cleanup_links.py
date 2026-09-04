import os, razorpay
from dotenv import load_dotenv

load_dotenv()
rzp = razorpay.Client(auth=(os.getenv("RZP_KEY_ID"),
                            os.getenv("RZP_KEY_SECRET")))

links = rzp.payment_link.all({"count": 100})["payment_links"]
killed = 0
for l in links:
    if l["status"] in ("created", "partially_paid"):
        try:
            rzp.payment_link.cancel(l["id"])
            killed += 1
        except Exception as e:
            print("skip", l["id"], e)
print(f"cancelled {killed} of {len(links)}")