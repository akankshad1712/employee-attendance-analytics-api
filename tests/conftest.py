"""
Test configuration and fixtures.

Uses a separate test database (sdt_attendance_test) so it never touches
production data. Both collections are wiped before every test function
to ensure isolation.

Atlas note: collection.drop() is asynchronous on shared clusters and can
race with the immediately-following create_index call. We use delete_many
instead, which is synchronous and leaves the collection + its indexes in
place, avoiding IndexBuildAborted / QueryPlanKilled errors.
"""
import os
import time
import pytest
from datetime import datetime, timezone, timedelta

# Load .env FIRST to get MONGO_URI, then override DB name before any app import
from dotenv import load_dotenv
load_dotenv()
os.environ["MONGO_DB"] = "sdt_attendance_test"

# Now import the app — it reads MONGO_DB from env at import time
from app.main import app, employees_col, logs_col, _create_indexes  # noqa: E402

from starlette.testclient import TestClient

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_ms() -> int:
    return int(datetime.now(UTC).timestamp()) * 1000


def ms(dt_str: str) -> int:
    """Parse 'YYYY-MM-DD HH:MM:SS IST' into epoch ms."""
    IST = timezone(timedelta(hours=5, minutes=30))
    dt  = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Session-scoped sync client (Starlette TestClient handles the lifespan)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Per-test DB reset — delete_many avoids the Atlas async-drop race
# ---------------------------------------------------------------------------
def _wipe():
    """Delete all documents from both test collections with majority write concern."""
    logs_col.with_options(
        write_concern=__import__("pymongo", fromlist=["WriteConcern"]).WriteConcern(w="majority")
    ).delete_many({})
    employees_col.with_options(
        write_concern=__import__("pymongo", fromlist=["WriteConcern"]).WriteConcern(w="majority")
    ).delete_many({})


@pytest.fixture(autouse=True)
def clean_db():
    """
    Wipe both test collections before AND after every test.
    Uses majority write concern so Atlas confirms the delete before the
    next test's setup or the next request begins.
    Does NOT drop the collections (avoids Atlas async IndexBuildAborted race).
    """
    _wipe()

    # Ensure indexes; retry on transient Atlas settling errors
    for attempt in range(3):
        try:
            _create_indexes()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)

    yield

    # Teardown: also wipe after, so the next test starts truly clean
    _wipe()
