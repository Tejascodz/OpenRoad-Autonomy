#!/usr/bin/env python3
"""First-run setup.

* creates `.env` from `.env.example` with a fresh random SECRET_KEY and Postgres password
* optionally creates the first admin user (password typed hidden, stored only as an Argon2 hash)

Safe to re-run: existing values in .env are kept unless you choose to regenerate.
"""
from __future__ import annotations

import os
import secrets
import stat
import subprocess  # nosec B404 - runs our own CLI with a fixed argv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def ask(prompt: str, default: str = "") -> str:
    val = input(f"{prompt} [{default}]: ").strip() if sys.stdin.isatty() else ""
    return val or default


def main() -> None:
    current = parse(ENV)
    values = dict(current)
    if not values.get("SECRET_KEY") or len(values["SECRET_KEY"]) < 32:
        values["SECRET_KEY"] = secrets.token_urlsafe(64)
        print("✓ generated SECRET_KEY")
    if not values.get("POSTGRES_PASSWORD"):
        values["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
        print("✓ generated POSTGRES_PASSWORD (docker-compose)")
    env = ""
    while env not in ("development", "production"):
        env = ask("Environment - type development or production", values.get("ENVIRONMENT", "development")).lower()
        if env not in ("development", "production"):
            print("  please type exactly: development  or  production  (or just press Enter)")
    values["DEBUG"] = "false"
    values["COOKIE_SECURE"] = "true" if values["ENVIRONMENT"] == "production" else values.get("COOKIE_SECURE", "false")
    values.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "")

    # Rewrite using the template order; keep unknown keys at the end.
    lines, seen = [], set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            seen.add(k)
            lines.append(f"{k}={values.get(k, line.split('=', 1)[1].strip())}")
        else:
            lines.append(line)
    extra = [f"{k}={v}" for k, v in values.items() if k not in seen]
    if extra:
        lines += ["", "# --- additional settings ---", *extra]
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(ENV, stat.S_IRUSR | stat.S_IWUSR)
    print(f"✓ wrote {ENV.name} (permissions 600, git-ignored)")

    if sys.stdin.isatty() and ask("Create an admin user now? (Y/n)", "y").lower() != "n":
        user = ask("Admin username", "admin")
        subprocess.run([sys.executable, "-m", "app.cli", "create-user", "--username", user, "--role", "admin"],
                       cwd=ROOT, check=False)  # nosec B603 - fixed argv, no shell
    print("\nNext: python -m app.main   ->  open http://127.0.0.1:8000")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled - nothing else was changed.")
