#!/usr/bin/env python3
"""
verify_signature.py — verify webhook signatures over the RAW request body (never re-serialize JSON first).

Frappe/ERPNext Webhook (enable_security=1):
    header X-Frappe-Webhook-Signature = base64( HMAC_SHA256(key=webhook_secret, msg=raw_body) )
GitHub (App or repo webhook):
    header X-Hub-Signature-256        = "sha256=" + hex( HMAC_SHA256(key=webhook_secret, msg=raw_body) )

CLI:
    python verify_signature.py frappe  --secret "$ERPNEXT_WEBHOOK_SECRET" --signature "$SIG" --body-file payload.bin
    python verify_signature.py github  --secret "$APP_WEBHOOK_SECRET"     --signature "$SIG" --body-file payload.bin
Exit 0 = valid, 1 = invalid.

Library:
    from verify_signature import verify_frappe, verify_github
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import sys


def verify_frappe(raw_body: bytes, header_signature: str | None, secret: str) -> bool:
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, (header_signature or "").strip())


def verify_github(raw_body: bytes, header_signature: str | None, secret: str) -> bool:
    if not header_signature or not header_signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_signature.strip())


def sign_frappe(raw_body: bytes, secret: str) -> str:
    """Produce the header value Frappe would send (useful for tests)."""
    return base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode()


def sign_github(raw_body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["frappe", "github"])
    ap.add_argument("--secret", required=True)
    ap.add_argument("--signature", required=True)
    ap.add_argument("--body-file", required=True, help="file with the exact raw bytes received ('-' for stdin)")
    a = ap.parse_args(argv)
    body = sys.stdin.buffer.read() if a.body_file == "-" else open(a.body_file, "rb").read()
    ok = (verify_frappe if a.kind == "frappe" else verify_github)(body, a.signature, a.secret)
    print("valid" if ok else "INVALID")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
