"""Put the shelves back before the suite runs.

The gate tests buy things, and buying now really does reduce stock — so
without this, `pytest` passes the first time and fails the fifth with a
string of 409s. Reseeding once per session keeps the suite repeatable.

This writes to the same database the running merchant reads, so it only
works when both use the same DB_URL (the default, sqlite:///sweets.db,
if you started uvicorn from the project root).
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def restock_the_shop():
    from merchant.seed import run
    run()
    yield