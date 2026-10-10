import hashlib
import hmac
import time


def sign(payload: bytes, secret: str, timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(payload: bytes, header: str, secret: str, tolerance=300):
    parts = [part.partition("=") for part in header.split(",")]
    times = [v for k, sep, v in parts if sep and k == "t"]
    signatures = [v for k, sep, v in parts if sep and k == "v1"]
    try:
        if len(times) != 1 or abs(time.time() - int(times[0])) > tolerance:
            return False
    except (ValueError, TypeError):
        return False
    expected = sign(payload, secret, int(times[0])).split("v1=", 1)[1]
    return any(hmac.compare_digest(expected, value) for value in signatures if value.isascii())
