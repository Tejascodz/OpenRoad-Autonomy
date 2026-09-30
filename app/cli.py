"""Admin CLI.

    python -m app.cli create-user --username admin --role admin
    python -m app.cli reset-password --username alice
    python -m app.cli list-users
    python -m app.cli unlock --username alice

Passwords are read with getpass (never from argv, which leaks into shell history / ps).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import settings
from .models.user import Role
from .security.passwords import hash_password, password_problems
from .services.database_service import get_db


def _ask_password(username: str) -> str:
    env = os.environ.get("OPENROAD_NEW_PASSWORD")  # for automation (CI / docker exec)
    if env:
        problems = password_problems(env, username)
        if problems:
            sys.exit("Password " + "; ".join(problems))
        return env
    print(f"Password rules: {settings.PASSWORD_MIN_LENGTH}+ characters, 3 of upper/lower/digit/symbol "
          f"(or a 20+ character passphrase), must not contain the username. Typing is hidden.")
    for attempt in range(3):
        pw = getpass.getpass("New password: ")
        problems = password_problems(pw, username)
        if problems:
            print("  ✗ Password " + "; ".join(problems) + " - try again")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("  ✗ Passwords do not match - try again")
            continue
        return pw
    sys.exit("Too many attempts; nothing was changed.")


def _check_network() -> None:
    import requests

    from .services.map_service import probe_overpass
    from .services.net import ca_file

    print(f"CA bundle: {ca_file()}")
    rows = [(f"Overpass (road map)  {u}", lambda u=u: probe_overpass(u)) for u in settings.OVERPASS_URLS]

    def get(url):
        try:
            r = requests.get(url, timeout=(6, 15), headers={"User-Agent": settings.OSM_USER_AGENT})
            return r.status_code < 400, f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    rows += [
        ("Nominatim (place search)", lambda: get(settings.NOMINATIM_URL + "?q=Bengaluru&format=json&limit=1")),
        ("Map tiles (browser map)", lambda: get("https://tile.openstreetmap.org/0/0/0.png")),
    ]
    any_overpass = False
    for name, fn in rows:
        ok, why = fn()
        any_overpass |= ok and name.startswith("Overpass")
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}  ({why})")
    if not any_overpass:
        print("\nNo road-map server reachable: check Wi-Fi / VPN / firewall / DNS, or use the offline demo map "
              "(OFFLINE_MAPS=true in .env, or the 'Use offline demo map' button in the dashboard).")


def _fetch_map(radius: int | None = None) -> None:
    if settings.OFFLINE_MAPS:
        sys.exit("OFFLINE_MAPS=true in .env - set it to false to download the real road map.")
    from .services.map_service import MapService

    print("Downloading the road map for the operating area. Progress is logged below; this can take a few")
    print("minutes when the OpenStreetMap servers are busy. Press Ctrl+C to stop.\n")
    if radius is not None and not 1000 <= radius <= 12000:
        sys.exit("--radius must be between 1000 and 12000 metres")
    svc = MapService()
    svc.warm_up(radius=radius, force_download=True)
    st = svc.status()
    if st["state"] == "ready":
        print(f"\n✓ Road map ready: {st['nodes']:,} junctions. Saved to data/graphs/ - the app now loads it "
              "instantly (restart it if it is running).")
    else:
        sys.exit(f"\n✗ {st['message']}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("--username", required=True)
    c.add_argument("--role", choices=[r.value for r in Role], default="viewer")
    r = sub.add_parser("reset-password")
    r.add_argument("--username", required=True)
    u = sub.add_parser("unlock")
    u.add_argument("--username", required=True)
    sub.add_parser("list-users")
    sub.add_parser("check-network", help="test connectivity to map / search servers")
    fm = sub.add_parser("fetch-map", help="download and save the operating-area road map now, with progress")
    fm.add_argument("--radius", type=int, default=None,
                    help="metres around the centre (default MAP_WARMUP_RADIUS_M, max 12000)")
    args = ap.parse_args(argv)
    if args.cmd == "check-network":
        return _check_network()
    if args.cmd == "fetch-map":
        return _fetch_map(args.radius)
    db = get_db()

    if args.cmd == "create-user":
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", args.username):
            sys.exit("Username must be 3-64 chars: letters, digits, _ . -")
        if db.get_user_by_name(args.username):
            sys.exit("User already exists")
        user = db.create_user(args.username, hash_password(_ask_password(args.username)), Role(args.role))
        db.audit("cli.user_create", actor="cli", target=user.username, detail={"role": args.role})
        print(f"Created {user.username} ({args.role})")
    elif args.cmd == "reset-password":
        user = db.get_user_by_name(args.username) or sys.exit("No such user")
        db.update_user(user.id, password_hash=hash_password(_ask_password(user.username)), failed_logins=0,
                       locked_until=None)
        db.audit("cli.password_reset", actor="cli", target=user.username)
        print("Password updated; existing sessions revoked")
    elif args.cmd == "unlock":
        user = db.get_user_by_name(args.username) or sys.exit("No such user")
        db.update_user(user.id, failed_logins=0, locked_until=None)
        print("Unlocked")
    elif args.cmd == "list-users":
        for row in db.list_users():
            print(f"{row['id']:>4}  {row['username']:<24} {row['role']:<9} active={row['is_active']} locked={row['locked']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
