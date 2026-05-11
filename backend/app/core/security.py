from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from typing import Any

from app.core.config import get_settings

HASH_ALGORITHM = "pbkdf2_sha256"
HASH_ITERATIONS = 260_000


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return f"{HASH_ALGORITHM}${HASH_ITERATIONS}${_b64_encode(salt)}${_b64_encode(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
        if algorithm != HASH_ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64_decode(salt), int(iterations))
        return hmac.compare_digest(_b64_encode(digest), expected)
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expires = timedelta(minutes=settings.auth_token_expire_minutes)
    now = int(time.time())
    payload = {"sub": subject, "iat": now, "exp": now + int(expires.total_seconds())}
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(settings.auth_secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64_encode(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        header_part, payload_part, signature_part = token.split(".", 2)
    except ValueError as exc:
        raise ValueError("Invalid token.") from exc

    signing_input = f"{header_part}.{payload_part}"
    expected_signature = hmac.new(settings.auth_secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64_encode(expected_signature), signature_part):
        raise ValueError("Invalid token signature.")

    payload = json.loads(_b64_decode(payload_part))
    expires_at = int(payload.get("exp", 0))
    subject = payload.get("sub")
    if expires_at < int(time.time()) or not subject:
        raise ValueError("Token expired.")
    return payload
