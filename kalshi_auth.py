"""
Kalshi API-key auth for authenticated endpoints (WebSocket handshake).

Env:
  KALSHI_API_KEY_ID       — key id from Kalshi
  KALSHI_PRIVATE_KEY_PATH — path to PEM private key
Optional:
  KALSHI_USE_DEMO=1 — use demo WebSocket host when building default WS URL
"""
import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

WS_SIGN_PATH = "/trade-api/ws/v2"


def load_private_key_pem(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_pss_sha256(private_key, message: str) -> str:
    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def websocket_auth_failure_reason() -> str | None:
    """
    If env looks partially set but headers cannot be built, return a specific reason.
    Returns None when vars are unset (caller shows generic help) or when the PEM path is valid.
    """
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        return None
    stripped = key_path.strip()
    if stripped.startswith("-----BEGIN") or "PRIVATE KEY" in stripped[:200]:
        return (
            "KALSHI_PRIVATE_KEY_PATH must be a file path (e.g. $HOME/.config/kalshi/key.pem), "
            "not the key text.\n"
            "Save the key into that file with nano, then export only the path."
        )
    path_obj = Path(key_path).expanduser()
    if not path_obj.is_file():
        return (
            f"Private key file not found: {key_path[:80]}{'…' if len(key_path) > 80 else ''}\n"
            "Set KALSHI_PRIVATE_KEY_PATH to the real .pem file path (not placeholder text)."
        )
    return None


def build_websocket_headers() -> dict[str, str] | None:
    """
    Headers for the WebSocket handshake. Returns None if credentials are missing.
    """
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        return None
    path_obj = Path(key_path).expanduser()
    if not path_obj.is_file():
        return None

    private_key = load_private_key_pem(str(path_obj))
    timestamp_ms = str(int(time.time() * 1000))
    sign_payload = timestamp_ms + "GET" + WS_SIGN_PATH
    signature = sign_pss_sha256(private_key, sign_payload)

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }
