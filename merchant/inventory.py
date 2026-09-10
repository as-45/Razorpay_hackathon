"""Stock movement — the only place that changes what is on the shelf.

Two agents can ask for the last box at the same instant, so a
read-then-write ("is there stock? ok, take it") is not safe. Every
movement here is a single conditional UPDATE:

    UPDATE products SET stock = stock - :qty
     WHERE id = :id AND stock >= :qty

The database decides. If another request got there first the row count
comes back 0 and this caller is told no. Works on SQLite and Postgres
alike, with no application-level locking.
"""

from sqlalchemy import update
from .models import Product


def reserve(db, lines):
    """Take `lines` off the shelf atomically.

    lines: [{"id": str, "qty": int}, ...]
    Returns (True, None) or (False, failed_product_id). On failure the
    session is rolled back, so a partly-filled basket never sticks.
    """
    for line in lines:
        pid, qty = line["id"], line["qty"]
        result = db.execute(
            update(Product)
            .where(Product.id == pid, Product.stock >= qty)
            .values(stock=Product.stock - qty)
            .execution_options(synchronize_session=False))
        if result.rowcount == 0:
            db.rollback()
            return False, pid
    db.commit()
    return True, None


def release(db, lines):
    """Put `lines` back on the shelf — an expired hold, or a cancelled order."""
    for line in lines:
        db.execute(
            update(Product)
            .where(Product.id == line["id"])
            .values(stock=Product.stock + line["qty"])
            .execution_options(synchronize_session=False))
    db.commit()
