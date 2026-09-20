#!/usr/bin/env python3
"""
gh_app_token.py — mint a GitHub App installation token and (optionally) fire repository_dispatch.

Flow: RS256 JWT (iat = now-60s, exp = now+9min, iss = client id) → GET /orgs/{org}/installation
      → POST /app/installations/{id}/access_tokens (scoped to repos + permissions) → token (1 h).

Env:  APP_CLIENT_ID (or APP_ID), APP_PRIVATE_KEY (PEM text; "\\n" escapes accepted) or APP_PRIVATE_KEY_PATH,
      GITHUB_INSTALLATION_ID (optional, skips lookup), GITHUB_API_URL (default https://api.github.com)

Examples
    export GH_TOKEN="$(python gh_app_token.py token --org fincosys --repos erpnext-sync --permission contents=write)"
    python gh_app_token.py dispatch --org fincosys --repo erpnext-sync --event-type erpnext_event \
        --payload '{"doctype":"Customer","name":"CUST-0001","event":"on_update"}'
    python gh_app_token.py installations          # list installations visible to the App (JWT auth)

Requires: pip install PyJWT cryptography   (the only non-stdlib dependency in this skill)
Never store the minted token anywhere persistent (ERPNext Webhook headers included): it expires in 1 hour.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
HDR = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
       "User-Agent": "erpnext-sync-skill"}


def _pem() -> str:
    pem = os.environ.get("APP_PRIVATE_KEY")
    path = os.environ.get("APP_PRIVATE_KEY_PATH")
    if not pem and path:
        pem = open(path).read()
    if not pem:
        raise SystemExit("APP_PRIVATE_KEY (PEM) or APP_PRIVATE_KEY_PATH is required")
    return pem.replace("\\n", "\n")


def app_jwt(client_id: str | None = None, pem: str | None = None) -> str:
    try:
        import jwt  # PyJWT
    except ImportError:
        raise SystemExit("pip install PyJWT cryptography")
    iss = client_id or os.environ.get("APP_CLIENT_ID") or os.environ.get("APP_ID")
    if not iss:
        raise SystemExit("APP_CLIENT_ID (preferred) or APP_ID is required")
    now = int(time.time())
    # iat 60 s in the past absorbs clock drift; exp must be <= now + 600 s
    return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": iss}, pem or _pem(), algorithm="RS256")


def _call(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method,
                                 headers={**HDR, "Authorization": f"Bearer {token}",
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text.strip() else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        raise SystemExit(f"GitHub {e.code} on {method} {path}: {text[:600]}")


def installation_id(bearer: str, org: str, repo: str | None = None) -> int:
    env = os.environ.get("GITHUB_INSTALLATION_ID")
    if env:
        return int(env)
    path = f"/repos/{org}/{repo}/installation" if repo else f"/orgs/{org}/installation"
    _, data = _call("GET", path, bearer)
    return int(data["id"])


def installation_token(org: str, repos: list[str] | None = None, permissions: dict | None = None,
                       repo_for_lookup: str | None = None) -> dict:
    j = app_jwt()
    inst = installation_id(j, org, repo_for_lookup or (repos[0] if repos else None))
    body: dict = {}
    if repos:
        body["repositories"] = repos
    if permissions:
        body["permissions"] = permissions
    status, data = _call("POST", f"/app/installations/{inst}/access_tokens", j, body)
    if status != 201:
        raise SystemExit(f"unexpected status {status} minting token")
    return data  # token, expires_at, permissions, repository_selection


def repository_dispatch(token: str, owner: str, repo: str, event_type: str, client_payload: dict) -> None:
    if len(client_payload) > 10:
        raise SystemExit("client_payload may have at most 10 top-level keys — nest under one key")
    if len(json.dumps(client_payload)) > 60_000:
        raise SystemExit("client_payload must stay under 64 KB — send identifiers, not whole documents")
    status, _ = _call("POST", f"/repos/{owner}/{repo}/dispatches", token,
                      {"event_type": event_type, "client_payload": client_payload})
    if status != 204:
        raise SystemExit(f"dispatch returned {status}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("jwt", help="print an App JWT (10 min)")
    sub.add_parser("installations", help="list installations (JWT auth)")
    p = sub.add_parser("token", help="print an installation token (1 h)")
    p.add_argument("--org", required=True); p.add_argument("--repos", help="comma list to scope down")
    p.add_argument("--permission", action="append", default=[], help="name=level, e.g. contents=write (repeatable)")
    p.add_argument("--json", action="store_true", help="print the full response instead of the bare token")
    p = sub.add_parser("dispatch", help="mint a scoped token and POST /repos/{org}/{repo}/dispatches")
    p.add_argument("--org", required=True); p.add_argument("--repo", required=True)
    p.add_argument("--event-type", default="erpnext_event"); p.add_argument("--payload", required=True, help="JSON or @file")
    a = ap.parse_args(argv)

    if a.cmd == "jwt":
        print(app_jwt())
    elif a.cmd == "installations":
        _, data = _call("GET", "/app/installations", app_jwt())
        print(json.dumps([{"id": i["id"], "account": i["account"]["login"], "repository_selection": i["repository_selection"]}
                          for i in data], indent=1))
    elif a.cmd == "token":
        perms = dict(p.split("=", 1) for p in a.permission) if a.permission else None
        repos = [r.strip() for r in a.repos.split(",")] if a.repos else None
        data = installation_token(a.org, repos, perms)
        print(json.dumps(data, indent=1) if a.json else data["token"])
    elif a.cmd == "dispatch":
        payload = a.payload
        payload = json.loads(open(payload[1:]).read()) if payload.startswith("@") else json.loads(payload)
        tok = installation_token(a.org, [a.repo], {"contents": "write"})["token"]
        repository_dispatch(tok, a.org, a.repo, a.event_type, payload)
        print(f"dispatched {a.event_type} to {a.org}/{a.repo}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
