"""Security regression tests: authn, authz, CSRF, tokens, rate limits, headers, CORS, XSS, uploads."""
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.main import app, create_app
from app.security.tokens import AUDIENCE, ISSUER, create_access_token
from app.services.database_service import get_db
from conftest import PASSWORDS, login, reset_fleet

ROOT = Path(__file__).resolve().parent.parent
TRIP = {"pickup_lat": 12.962, "pickup_lon": 77.585, "delivery_lat": 12.978, "delivery_lon": 77.600}
PUBLIC = {"/", "/login", "/favicon.ico", "/health", "/ready", "/api/v1/auth/login", "/api/v1/auth/token",
          "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


# ----------------------------------------------------------------- authentication
def test_every_api_route_requires_auth(client):
    client.cookies.clear()
    checked = 0
    for path, ops in app.openapi()["paths"].items():
        if path in PUBLIC:
            continue
        for m in ops:
            resp = client.request(m.upper(), _fill(path), json={})
            assert resp.status_code == 401, f"{m} {path} -> {resp.status_code}"
            checked += 1
    assert checked >= 20


def test_login_bad_credentials_generic_error(client):
    client.cookies.clear()
    r1 = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "wrong-password-123"})
    r2 = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong-password-123"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()  # no username enumeration


def test_sql_injection_style_username_rejected(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin' OR '1'='1", "password": "x' OR '1'='1"})
    assert r.status_code == 401


def test_session_cookie_flags(client):
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"username": "viewer", "password": PASSWORDS["viewer"]})
    cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in cookies if c.startswith("or_session="))
    assert "HttpOnly" in sess and "SameSite=strict" in sess.replace("Strict", "strict") and "Path=/" in sess
    assert "access_token" not in r.json()  # token never exposed to JS


def test_lockout_after_repeated_failures(client):
    db = get_db()
    from app.models.user import Role
    from app.security.passwords import hash_password
    if not db.get_user_by_name("locktest"):
        db.create_user("locktest", hash_password("L0ck-Test-Pass-99!"), Role.VIEWER)
    from app.security.rate_limit import limiter
    for _ in range(settings.LOGIN_MAX_FAILURES):
        limiter.clear()
        client.post("/api/v1/auth/login", json={"username": "locktest", "password": "bad-password-000"})
    limiter.clear()
    r = client.post("/api/v1/auth/login", json={"username": "locktest", "password": "L0ck-Test-Pass-99!"})
    assert r.status_code == 423  # locked even with the right password
    u = db.get_user_by_name("locktest")
    db.update_user(u.id, failed_logins=0, locked_until=None)


def test_login_rate_limited(client):
    client.cookies.clear()
    codes = [client.post("/api/v1/auth/login", json={"username": "x" * 5, "password": "y" * 12}).status_code
             for _ in range(8)]
    assert 429 in codes
    r = [c for c in codes if c == 429]
    assert r


# ----------------------------------------------------------------- tokens
def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_bearer_token_flow_and_no_csrf_needed(client):
    client.cookies.clear()
    r = client.post("/api/v1/auth/token", data={"username": "operator", "password": PASSWORDS["operator"]})
    assert r.status_code == 200
    tok = r.json()["access_token"]
    client.cookies.clear()
    assert client.get("/api/v1/fleet", headers=_bearer(tok)).status_code == 200
    assert client.post("/api/v1/plan", json=TRIP, headers=_bearer(tok)).status_code == 200


@pytest.mark.parametrize("kind", ["none_alg", "wrong_key", "expired", "wrong_aud", "garbage", "tampered_role"])
def test_forged_tokens_rejected(client, kind):
    client.cookies.clear()
    viewer = get_db().get_user_by_name("viewer")
    now = datetime.now(timezone.utc)
    claims = {"sub": str(viewer.id), "role": "admin", "tv": viewer.token_version, "jti": "x", "iat": now,
              "nbf": now, "exp": now + timedelta(minutes=5), "iss": ISSUER, "aud": AUDIENCE, "typ": "access"}
    key = settings.SECRET_KEY.get_secret_value()
    if kind == "none_alg":
        tok = jwt.encode(claims, None, algorithm="none") if hasattr(jwt, "encode") else "x"
    elif kind == "wrong_key":
        tok = jwt.encode(claims, "not-the-key-" * 4, algorithm="HS256")
    elif kind == "expired":
        claims.update(iat=now - timedelta(hours=2), nbf=now - timedelta(hours=2), exp=now - timedelta(hours=1))
        tok = jwt.encode(claims, key, algorithm="HS256")
    elif kind == "wrong_aud":
        claims["aud"] = "someone-else"
        tok = jwt.encode(claims, key, algorithm="HS256")
    elif kind == "garbage":
        tok = "a.b.c"
    else:  # valid viewer token with role claim edited -> signature breaks
        good, _ = create_access_token(viewer.id, "viewer", viewer.token_version)
        h, p, s = good.split(".")
        import base64
        import json
        payload = json.loads(base64.urlsafe_b64decode(p + "=="))
        payload["role"] = "admin"
        p2 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        tok = f"{h}.{p2}.{s}"
    assert client.get("/api/v1/fleet", headers=_bearer(tok)).status_code == 401


def test_role_claim_is_not_trusted(client):
    """Even a validly signed token claiming admin only gets the DB role."""
    viewer = get_db().get_user_by_name("viewer")
    tok, _ = create_access_token(viewer.id, "admin", viewer.token_version)
    client.cookies.clear()
    assert client.get("/api/v1/admin/users", headers=_bearer(tok)).status_code == 403


def test_password_change_revokes_old_tokens(client):
    db = get_db()
    from app.models.user import Role
    from app.security.passwords import hash_password
    if not db.get_user_by_name("rotator"):
        db.create_user("rotator", hash_password("R0tate-Me-Please-1!"), Role.VIEWER)
    client.cookies.clear()
    tok = client.post("/api/v1/auth/token", data={"username": "rotator", "password": "R0tate-Me-Please-1!"}).json()["access_token"]
    r = client.post("/api/v1/auth/change-password", headers=_bearer(tok),
                    json={"current_password": "R0tate-Me-Please-1!", "new_password": "N3w-Rotated-Pass-2!"})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    assert client.get("/api/v1/auth/me", headers=_bearer(tok)).status_code == 401


def test_disabled_user_loses_access_immediately(client):
    db = get_db()
    from app.models.user import Role
    from app.security.passwords import hash_password
    u = db.get_user_by_name("tempuser") or db.create_user("tempuser", hash_password("T3mp-User-Pass-9!"), Role.OPERATOR)
    tok, _ = create_access_token(u.id, "operator", db.get_user(u.id).token_version)
    client.cookies.clear()
    assert client.get("/api/v1/auth/me", headers=_bearer(tok)).status_code == 200
    db.update_user(u.id, is_active=False)
    assert client.get("/api/v1/auth/me", headers=_bearer(tok)).status_code == 401


# ----------------------------------------------------------------- authorisation (RBAC)
def test_viewer_permissions(client):
    csrf = login(client, "viewer")
    h = {"X-CSRF-Token": csrf}
    assert client.get("/api/v1/fleet").status_code == 200
    assert client.post("/api/v1/plan", json=TRIP, headers=h).status_code == 403
    assert client.post("/api/v1/start_delivery", json=TRIP, headers=h).status_code == 403
    assert client.post("/api/v1/robots/R001/resume", headers=h).status_code == 403
    assert client.post("/api/v1/robots/R001/cancel", headers=h).status_code == 403
    assert client.post("/api/v1/fleet/sim_speed", json={"scale": 2}, headers=h).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.get("/api/v1/system/info").status_code == 403
    # safety: anyone signed in can hit the emergency stop ...
    assert client.post("/api/v1/robots/R001/stop", headers=h).status_code == 200
    assert client.post("/api/v1/fleet/stop_all", headers=h).status_code == 200
    assert client.post("/api/v1/fleet/resume_all", headers=h).status_code == 403
    # ... but only operators can release it
    op = login(client, "operator")
    assert client.post("/api/v1/fleet/resume_all", headers={"X-CSRF-Token": op}).status_code == 200
    assert client.post("/api/v1/robots/R999/stop", headers={"X-CSRF-Token": op}).status_code == 404
    assert client.post("/api/v1/robots/..%2Fadmin/stop", headers={"X-CSRF-Token": op}).status_code in (404, 422)


def test_operator_cannot_reach_admin(client):
    csrf = login(client, "operator")
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.post("/api/v1/admin/users", json={"username": "evil", "password": "Evil-Passw0rd-123!",
                                                    "role": "admin"}, headers={"X-CSRF-Token": csrf}).status_code == 403


def test_admin_user_management_and_guards(client):
    csrf = login(client, "admin")
    h = {"X-CSRF-Token": csrf}
    r = client.post("/api/v1/admin/users", json={"username": "newop", "password": "short", "role": "operator"}, headers=h)
    assert r.status_code == 422  # password policy
    r = client.post("/api/v1/admin/users", json={"username": "newop", "password": "N3w-Operator-Pass!", "role": "operator"}, headers=h)
    assert r.status_code in (201, 409)
    r = client.post("/api/v1/admin/users", json={"username": "<script>", "password": "N3w-Operator-Pass!"}, headers=h)
    assert r.status_code == 422  # username pattern
    me = client.get("/api/v1/auth/me").json()
    assert client.patch(f"/api/v1/admin/users/{me['id']}", json={"role": "viewer"}, headers=h).status_code == 400
    assert client.delete(f"/api/v1/admin/users/{me['id']}", headers=h).status_code == 400
    assert client.patch("/api/v1/admin/users/1", json={"password_hash": "x"}, headers=h).status_code == 422
    audit = client.get("/api/v1/admin/audit").json()
    assert any(a["action"] == "admin.user_create" for a in audit)
    assert all("password" not in str(a.get("detail") or "").lower() for a in audit)


# ----------------------------------------------------------------- CSRF / origin / CORS
def test_csrf_required_for_cookie_sessions(client):
    login(client, "operator")
    assert client.post("/api/v1/plan", json=TRIP).status_code == 403
    assert client.post("/api/v1/plan", json=TRIP, headers={"X-CSRF-Token": "forged"}).status_code == 403


def test_cross_origin_post_blocked(client):
    csrf = login(client, "operator")
    r = client.post("/api/v1/plan", json=TRIP, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/v1/plan", json=TRIP, headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert r.status_code == 200


def test_cors_does_not_reflect_arbitrary_origins(client):
    r = client.options("/api/v1/fleet", headers={"Origin": "https://evil.example",
                                                        "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") not in ("*", "https://evil.example")
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_untrusted_host_rejected(client):
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


# ----------------------------------------------------------------- headers / debug / exposure
def test_security_headers_present(client):
    r = client.get("/")
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "server" not in h
    assert client.get("/api/v1/fleet").headers.get("cache-control") == "no-store"


def test_production_mode_hides_docs_and_debug(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    prod = create_app()
    assert prod.docs_url is None and prod.redoc_url is None and prod.openapi_url is None
    assert prod.debug is False


def test_no_path_traversal_or_exposed_files(client):
    for p in ["/static/../app/config.py", "/static/%2e%2e/app/config.py", "/.env", "/static/../../.env",
              "/.git/config", "/data/deliveries.db", "/logs/robot.log", "/static/js/../../../app/main.py"]:
        assert client.get(p).status_code == 404, p


def test_errors_do_not_leak_internals(client):
    csrf = login(client, "operator")
    r = client.post("/api/v1/plan", json={**TRIP, "pickup_lat": "NaN"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422
    body = r.text
    assert "input" not in r.json()["detail"][0] and "Traceback" not in body
    r = client.post("/api/v1/plan", json={**TRIP, "evil": 1}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422  # unknown fields forbidden


def test_input_bounds_and_geofence(client):
    csrf = login(client, "operator")
    h = {"X-CSRF-Token": csrf}
    assert client.post("/api/v1/plan", json={**TRIP, "pickup_lat": 91}, headers=h).status_code == 422
    assert client.post("/api/v1/plan", json={**TRIP, "algorithm": "bogus"}, headers=h).status_code == 422
    far = {"pickup_lat": 40.7, "pickup_lon": -74.0, "delivery_lat": 40.71, "delivery_lon": -74.01}
    r = client.post("/api/v1/plan", json=far, headers=h)
    assert r.status_code == 422 and "geofence" in r.text
    assert client.get("/api/v1/deliveries/history?limit=100000").status_code == 422
    assert client.get("/api/v1/deliveries/0").status_code == 422


def test_body_size_limit(client):
    csrf = login(client, "operator")
    big = "x" * (settings.MAX_BODY_BYTES + 10)
    r = client.post("/api/v1/plan", content=big, headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
    assert r.status_code == 413


# ----------------------------------------------------------------- XSS hygiene of the frontend
def test_frontend_has_no_inline_code_or_innerhtml():
    static = ROOT / "app" / "static"
    for html in static.glob("*.html"):
        text = html.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), f"inline script in {html.name}"
        assert " style=" not in text and "<style" not in text, f"inline style in {html.name}"
        assert not re.search(r"\son[a-z]+=", text), f"inline event handler in {html.name}"
        assert "https://" not in text.split("<body")[0], f"third-party asset in {html.name}"
    for js in (static / "js").glob("*.js"):
        text = js.read_text()
        assert "innerHTML" not in text and "outerHTML" not in text and "insertAdjacentHTML" not in text, js.name
        assert "eval(" not in text and "document.write" not in text, js.name


# ----------------------------------------------------------------- websocket
def test_websocket_requires_auth_and_same_origin(client):
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/v1/ws/fleet", headers={"Origin": "http://testserver"}) as ws:
            ws.receive_text()
    assert e.value.code == 4401
    login(client, "viewer")
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/v1/ws/fleet", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 1008
    with client.websocket_connect("/api/v1/ws/fleet", headers={"Origin": "http://testserver"}) as ws:
        first = ws.receive_json()
        assert first["type"] == "fleet" and len(first["data"]["robots"]) >= 1
        ws.send_text('{"type":"ping"}')
        assert ws.receive_json()["type"] in ("pong", "fleet")


# ----------------------------------------------------------------- fleet dispatch through the API
def test_fleet_dispatch_lifecycle_via_api(client):
    csrf = login(client, "operator")
    h = {"X-CSRF-Token": csrf}
    reset_fleet(client, csrf)
    robots = [r["id"] for r in client.get("/api/v1/fleet").json()["robots"]]
    assigned, dids = set(), []
    for _ in robots:
        r = client.post("/api/v1/start_delivery", json=TRIP, headers=h)
        assert r.status_code == 200, r.text
        assigned.add(r.json()["robot_id"])
        dids.append(r.json()["delivery_id"])
    assert assigned == set(robots)                      # each robot got one job
    busy = client.post("/api/v1/start_delivery", json=TRIP, headers=h)
    assert busy.status_code == 409 and "no robot is available" in busy.json()["detail"]
    d = client.get(f"/api/v1/deliveries/{dids[0]}").json()
    assert isinstance(d["path_planned"], list) and d["robot_id"] in robots and d["status"] == "pickup"
    route = client.get(f"/api/v1/robots/{d['robot_id']}/route").json()
    assert route["legs"] and route["legs"][-1]["kind"] == "to_dropoff"
    assert client.post(f"/api/v1/robots/{d['robot_id']}/cancel", headers=h).status_code == 200
    time.sleep(0.3)
    assert client.get(f"/api/v1/deliveries/{dids[0]}").json()["status"] == "cancelled"
    reset_fleet(client, csrf)


def test_specific_robot_dispatch_and_validation(client):
    csrf = login(client, "operator")
    h = {"X-CSRF-Token": csrf}
    reset_fleet(client, csrf)
    r = client.post("/api/v1/start_delivery", json={**TRIP, "robot_id": "R002"}, headers=h)
    assert r.status_code == 200 and r.json()["robot_id"] == "R002"
    again = client.post("/api/v1/start_delivery", json={**TRIP, "robot_id": "R002"}, headers=h)
    assert again.status_code == 409
    assert client.post("/api/v1/start_delivery", json={**TRIP, "robot_id": "<script>"}, headers=h).status_code == 422
    outside = {**TRIP, "delivery_lat": 13.02}           # inside geofence, outside the loaded map
    r = client.post("/api/v1/plan", json=outside, headers=h)
    assert r.status_code == 422 and "outside the loaded road map" in r.json()["detail"]
    reset_fleet(client, csrf)


def test_admin_can_add_and_remove_robots(client):
    csrf = login(client, "operator")
    assert client.post("/api/v1/admin/robots", json={}, headers={"X-CSRF-Token": csrf}).status_code == 403
    csrf = login(client, "admin")
    h = {"X-CSRF-Token": csrf}
    r = client.post("/api/v1/admin/robots", json={"name": "Night shift"}, headers=h)
    assert r.status_code == 201
    rid = r.json()["id"]
    assert rid in [x["id"] for x in client.get("/api/v1/fleet").json()["robots"]]
    assert client.post("/api/v1/admin/robots", json={"name": "<b>x</b>"}, headers=h).status_code == 422
    assert client.delete(f"/api/v1/admin/robots/{rid}", headers=h).status_code == 200
    assert rid not in [x["id"] for x in client.get("/api/v1/fleet").json()["robots"]]
