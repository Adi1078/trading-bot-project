"""
UI access control (the client's login page) — completely separate from the 5paisa
broker OAuth in routes/auth.py.

Design (per client requirement): a device that logs in once stays logged in
"forever" — until that browser's cookies/cache are cleared. We achieve this with a
long-lived signed cookie:

  * The password is stored only as a salted PBKDF2 hash (stdlib hashlib — no extra
    native dependency like bcrypt/passlib).
  * On login we set a signed cookie (itsdangerous) that proves "this device passed
    auth". The cookie carries a 10-year max-age, so the browser keeps it across
    refreshes/reboots and only drops it when the user clears their cache.
  * The signing secret lives in the DB (gitignored), generated once. We deliberately
    do NOT rotate it when the password changes, so changing the password does not
    log existing trusted devices out (matches the chosen behaviour).
"""
import hashlib
import hmac
import secrets

from itsdangerous import URLSafeSerializer

COOKIE_NAME = "bot_auth"
# ~10 years in seconds — effectively "until the browser cache is cleared".
COOKIE_MAX_AGE = 10 * 365 * 24 * 60 * 60
_SALT = "bot-ui-auth"
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Return a self-describing 'pbkdf2_sha256$iters$salt$hash' string."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def make_token(secret: str, username: str) -> str:
    """Sign a cookie value that identifies an authenticated device."""
    return URLSafeSerializer(secret, salt=_SALT).dumps({"u": username})


def verify_token(secret: str, token: str):
    """Return the username if the cookie is a valid signed token, else None."""
    try:
        data = URLSafeSerializer(secret, salt=_SALT).loads(token)
        return data.get("u")
    except Exception:
        return None
