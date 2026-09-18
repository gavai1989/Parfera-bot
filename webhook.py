from __future__ import annotations

import hmac


def build_webhook_url(base_url: str, secret_path: str) -> str:
    base = base_url.rstrip("/")
    path = secret_path.strip("/")
    if not base or not path:
        raise ValueError("base_url and secret_path are required")
    return f"{base}/webhook/{path}"


def is_valid_webhook_secret(expected: str, received: str) -> bool:
    if not expected or not received:
        return False
    return hmac.compare_digest(expected, received)
