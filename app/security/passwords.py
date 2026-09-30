"""Password hashing (Argon2id, RFC 9106) and password policy."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import settings

# argon2-cffi defaults = RFC 9106 "low memory" profile: Argon2id, t=3, m=64 MiB, p=4.
_hasher = PasswordHasher()

# A throw-away hash so that login for an unknown user costs the same time as a real one
# (prevents username enumeration via response timing).
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")

_COMMON = {
    "password", "password123", "123456789012", "qwertyuiopas", "adminadmin12", "letmein12345",
    "changeme1234", "welcome12345", "iloveyou1234", "passwordpassword", "administrator",
    "000000000000", "111111111111", "123412341234", "abc123abc123", "p@ssw0rd1234",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def password_problems(password: str, username: str = "") -> list[str]:
    problems = []
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        problems.append(f"must be at least {settings.PASSWORD_MIN_LENGTH} characters")
    if len(password) > 128:
        problems.append("must be at most 128 characters")
    if password.lower() in _COMMON:
        problems.append("is too common")
    if username and username.lower() in password.lower():
        problems.append("must not contain the username")
    classes = sum([any(c.islower() for c in password), any(c.isupper() for c in password),
                   any(c.isdigit() for c in password), any(not c.isalnum() for c in password)])
    if classes < 3 and len(password) < 20:
        problems.append("needs 3 of: lowercase, uppercase, digit, symbol (or use a 20+ character passphrase)")
    return problems
