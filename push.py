"""Web Push notifications for the installed TimGym app.

The server signs each push with a VAPID key pair that is created once and kept
next to the database. Browsers (on iPhone: the Home Screen app, iOS 16.4+)
subscribe with the public key; pushes go to the endpoint each subscription names.
"""
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
import requests
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush


def load_or_create_vapid(data_dir):
    """Return the VAPID key pair, creating and storing it on first use."""
    path = Path(data_dir) / "vapid_private.pem"
    if path.exists():
        return Vapid02.from_file(str(path))
    vapid = Vapid02()
    vapid.generate_keys()
    pem = vapid.private_pem()
    # Publish atomically so concurrent workers end up with the same key.
    candidate = path.with_suffix(f".{os.getpid()}.tmp")
    candidate.write_bytes(pem)
    try:
        candidate.chmod(0o600)
    except OSError:
        pass
    try:
        os.link(candidate, path)
    except FileExistsError:
        pass
    finally:
        candidate.unlink(missing_ok=True)
    return Vapid02.from_file(str(path))


def public_key(vapid):
    """The applicationServerKey browsers need: base64url of the uncompressed point."""
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _service(endpoint):
    host = endpoint.split("/")[2] if endpoint.count("/") >= 2 else endpoint
    return "Apple" if host.endswith("push.apple.com") else host


def send(vapid, subscriptions, payload, subject, errors=None):
    """Push payload to each subscription. Returns (delivered, gone_endpoints).

    If errors is a list, a short reason is appended for each failed delivery.

    subject identifies the sender to push services (Apple requires an https: or
    mailto: URL). Subscriptions the push service reports as gone (404/410) are
    returned so the caller can delete them.
    """
    delivered, gone = 0, []
    body = json.dumps(payload)
    for subscription in subscriptions:
        info = {"endpoint": subscription["endpoint"], "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}}
        try:
            webpush(info, data=body, vapid_private_key=vapid, vapid_claims={"sub": subject}, ttl=3600, timeout=10)
            delivered += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                gone.append(subscription["endpoint"])
            if errors is not None:
                detail = (getattr(exc.response, "text", "") or str(exc))[:200]
                errors.append(f"{_service(subscription['endpoint'])} answered {status}: {detail}")
        except requests.RequestException as exc:
            # Unreachable push service; try again next time.
            if errors is not None:
                errors.append(f"{_service(subscription['endpoint'])} unreachable: {type(exc).__name__}")
    return delivered, gone
