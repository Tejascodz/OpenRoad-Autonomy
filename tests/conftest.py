import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="openroad-test-"))
os.environ.update({
    "ENVIRONMENT": "test",
    "OFFLINE_MAPS": "true",
    "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "LOG_DIR": str(_TMP / "logs"),
    "COOKIE_SECURE": "false",
    "FLEET_SIZE": "3",
    "SIM_TIME_SCALE": "20",
    "LOG_LEVEL": "WARNING",
    "RATE_LIMIT_PER_MIN": "100000",
})
os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.user import Role  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402
from app.security.rate_limit import limiter  # noqa: E402
from app.services.database_service import get_db  # noqa: E402

PASSWORDS = {"admin": "Adm1n-Str0ng-Pass!", "operator": "0perator-Pass-2026!", "viewer": "V1ewer-Pass-2026!"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        db = get_db()
        for name, pw in PASSWORDS.items():
            if not db.get_user_by_name(name):
                db.create_user(name, hash_password(pw), Role(name))
        yield c


@pytest.fixture(autouse=True)
def _reset_limits():
    limiter.clear()
    yield
    limiter.clear()


def login(client, username):
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def reset_fleet(client, csrf):
    """Release stops and cancel all missions (tests share one app / fleet)."""
    h = {"X-CSRF-Token": csrf}
    client.post("/api/v1/fleet/resume_all", headers=h)
    for r in client.get("/api/v1/fleet").json()["robots"]:
        if r["delivery_id"] is not None or r["mode"] not in ("idle", "charging"):
            client.post(f"/api/v1/robots/{r['id']}/cancel", headers=h)
