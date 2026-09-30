# Security

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub → **Security** → **Report a vulnerability** on this
repository, not as a public issue.

## Security design

**Authentication & sessions**

- Every `/api/v1` route and the WebSocket require a session (a test enumerates all routes from the OpenAPI
  schema and asserts `401` without credentials).
- Browser sessions: HS256 JWT (issuer, audience, expiry, `jti`) in an HttpOnly, SameSite=Strict cookie
  (+ `Secure` in production), 30-minute lifetime. Scripts can use `Authorization: Bearer <token>`.
- The role is re-read from the database on every request; a per-user `token_version` revokes all sessions on
  password change, role change, disable or "log out everywhere".

**Passwords**

- Argon2id (RFC 9106 low-memory profile), re-hashed automatically when parameters change.
- Policy: at least 12 characters and 3 character classes (or a 20+ character passphrase); common passwords and
  passwords containing the username are rejected.
- Generic login errors, a dummy hash for unknown users (no username enumeration by timing), per-IP and
  per-account throttling, 15-minute lockout after 5 failures.

**Access control** — roles `viewer` / `operator` / `admin`. Anyone signed in can press STOP. Admins cannot
demote, disable or delete themselves, and the last active admin cannot be removed. Security-relevant actions
are written to an audit log (no secrets stored).

**Request handling**

- CSRF: session-bound HMAC token required on cookie-authenticated `POST` / `PATCH` / `DELETE`, plus an Origin
  check for unsafe methods.
- CORS: explicit allow-list from `CORS_ORIGINS` (a wildcard is rejected at startup). `TrustedHostMiddleware`
  guards the Host header.
- Rate limits per IP and route (login, planning, dispatch, place search, WebSocket, global), 1 MB body limit.
- Input validation on every endpoint: coordinate bounds, NaN, unknown fields, trip length, operating area.
- Generic error messages (no stack traces or internal details in responses). Interactive API docs are disabled
  when `ENVIRONMENT=production`.

**Browser** — strict Content-Security-Policy (no inline scripts or styles), Leaflet self-hosted,
X-Frame-Options `DENY`, `nosniff`, Referrer-Policy, Permissions-Policy, COOP/CORP, HSTS in production,
`Cache-Control: no-store` on the API. The dashboard never builds HTML from server data (a test fails the build
if `innerHTML`, `eval` or `document.write` appear).

**WebSocket** — cookie authentication, same-origin check, per-IP and global connection caps, message size and
rate limits; sockets close when the session expires.

**Secrets & data**

- Secrets live only in `.env` (git-ignored, created with mode 600 by `scripts/setup_env.py`) or in environment
  variables. `.env.example` contains no values. `SECRET_KEY` is mandatory in production.
- No third-party API key is required: maps come from OpenStreetMap.
- SQLite file mode 600 (data directory 700); ORM-only queries; database passwords are masked in logs.

**Deployment (Docker)** — non-root user, read-only root filesystem, `no-new-privileges`, all capabilities
dropped, `.dockerignore` keeps `.env` and `.git` out of the image. Only nginx (TLS 1.2/1.3) is published; the
API and Postgres (SCRAM auth, random password) stay on an internal network.

## Automated checks

Run on every push and pull request (`.github/workflows/ci.yml`):

| Check | Tool |
|---|---|
| Tests (incl. security tests) | `pytest` |
| Static analysis | `bandit -r app scripts` |
| Known-vulnerable dependencies | `pip-audit` |
| Secrets in the full git history | `gitleaks` |

A `detect-secrets` pre-commit hook (`.pre-commit-config.yaml`) blocks secrets before they are committed.

## Recommendations for real deployments

1. Serve over HTTPS (the provided nginx config); cookies are `Secure` only then.
2. Set `TRUSTED_HOSTS` and `CORS_ORIGINS` to your real domain(s).
3. Rate limits are in-memory (exact for the single-process design); move them to Redis if you run several workers.
4. Consider TOTP two-factor authentication for admins (not implemented).
5. Use your own tile server or a tile provider for heavy use (OpenStreetMap tile usage policy).
6. The fleet is simulated and there is no obstacle detection or collision avoidance: real robots need their own
   certified on-board safety system.
