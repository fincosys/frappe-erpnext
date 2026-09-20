#!/usr/bin/env python3
"""
frappe_client.py — stdlib-only CLI + library for the Frappe / ERPNext REST API (v15 and v16).

Credentials come from env vars (never from files):
    ERPNEXT_URL          https://yoursite.frappe.cloud
    ERPNEXT_API_KEY      from User > Settings > API Access
    ERPNEXT_API_SECRET

Mutating commands (insert, update, set-value, delete, cancel, submit, merge, insert-many,
data-import, tag, POST call) are DRY-RUN by default: they print the request they would send and
exit 0. Add --apply to actually send. Read-only commands always run.

Examples
    python frappe_client.py versions
    python frappe_client.py whoami
    python frappe_client.py count Customer --filters '{"disabled":0}'
    python frappe_client.py list Customer --fields name,customer_name,tax_id --all --out customers.jsonl
    python frappe_client.py groupcount Contact email_id --filters '[["email_id","is","set"]]'
    python frappe_client.py get "Sales Invoice" ACC-SINV-2026-00012 --docinfo
    python frappe_client.py merge Customer "Acme Ltd - 1" "Acme Ltd" --snapshot-dir snapshots/ --apply
    python frappe_client.py cancel "Bank Transaction" ACC-BTN-2026-00099 --apply
    python frappe_client.py insert Webhook --json @webhook.json --apply
    python frappe_client.py call frappe.client.get_value --get --args '{"doctype":"Customer","filters":{"tax_id":"X"},"fieldname":["name"]}'
    python frappe_client.py export Customer --fields name,customer_name,modified --csv customers.csv
    python frappe_client.py webhook-log customer-to-github --limit 20

Library use
    from frappe_client import Frappe
    fr = Frappe.from_env(dry_run=False)
    for row in fr.iter_all("Item", fields=["name", "item_name"]): ...
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_PAGE = 500
RETRY_STATUSES = {429, 502, 503, 504}


class FrappeError(Exception):
    """HTTP/business error from Frappe with the server messages unpacked."""

    def __init__(self, status: int, exc_type: str | None, messages: list[str], raw: dict | str | None = None):
        self.status = status
        self.exc_type = exc_type
        self.messages = messages
        self.raw = raw
        super().__init__(f"HTTP {status} {exc_type or ''}: {'; '.join(messages) or raw}")


def _unpack_server_messages(body: dict) -> list[str]:
    """_server_messages is a JSON string of JSON strings: [ "{\"message\": ...}" ]."""
    out: list[str] = []
    raw = body.get("_server_messages")
    if raw:
        try:
            for m in json.loads(raw):
                try:
                    m = json.loads(m)
                    out.append(str(m.get("message") or m))
                except (TypeError, ValueError):
                    out.append(str(m))
        except (TypeError, ValueError):
            out.append(str(raw))
    # v2 error shape
    for e in body.get("errors") or []:
        if isinstance(e, dict):
            out.append(str(e.get("message") or e))
    for m in body.get("messages") or []:
        out.append(str(m))
    if not out and body.get("exception"):
        out.append(str(body["exception"]).splitlines()[-1])
    return out


def _jsonify_params(params: dict | None) -> dict:
    """Frappe wants fields/filters/or_filters/docs/etc. as JSON strings in the query/form."""
    if not params:
        return {}
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (dict, list, tuple)):
            out[k] = json.dumps(v)
        elif isinstance(v, bool):
            out[k] = int(v)
        else:
            out[k] = v
    return out


class Frappe:
    """Thin, explicit client. Every method maps to one documented endpoint."""

    def __init__(self, url: str, api_key: str, api_secret: str, *, dry_run: bool = True,
                 timeout: int = 60, verbose: bool = False, max_retries: int = 5):
        if not url or not api_key or not api_secret:
            raise SystemExit("Missing ERPNEXT_URL / ERPNEXT_API_KEY / ERPNEXT_API_SECRET")
        self.url = url.rstrip("/")
        self.auth = f"token {api_key}:{api_secret}"
        self.dry_run = dry_run
        self.timeout = timeout
        self.verbose = verbose
        self.max_retries = max_retries
        self._versions: dict | None = None

    @classmethod
    def from_env(cls, **kw) -> "Frappe":
        return cls(os.environ.get("ERPNEXT_URL", ""), os.environ.get("ERPNEXT_API_KEY", ""),
                   os.environ.get("ERPNEXT_API_SECRET", ""), **kw)

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, *, params: dict | None = None, form: dict | None = None,
                 json_body=None, mutating: bool = False, raw_body: bytes | None = None,
                 content_type: str | None = None, stream_to: pathlib.Path | None = None):
        qs = urllib.parse.urlencode(_jsonify_params(params), doseq=False) if params else ""
        url = f"{self.url}{path}" + (f"?{qs}" if qs else "")
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(_jsonify_params(form)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type or "application/octet-stream"

        if mutating and self.dry_run:
            plan = {"dry_run": True, "method": method, "url": url,
                    "body": json_body if json_body is not None else (form or (f"<{len(raw_body)} bytes>" if raw_body else None))}
            print(f"[dry-run] {method} {url}", file=sys.stderr)
            return plan

        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            if self.verbose:
                print(f"> {method} {url}", file=sys.stderr)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if stream_to is not None:
                        stream_to.write_bytes(resp.read())
                        return {"saved": str(stream_to)}
                    text = resp.read().decode("utf-8", "replace")
                    return json.loads(text) if text.strip() else {}
            except urllib.error.HTTPError as e:
                status = e.code
                text = e.read().decode("utf-8", "replace")
                if status in RETRY_STATUSES and attempt < self.max_retries:
                    wait = e.headers.get("Retry-After")
                    delay = float(wait) if wait and wait.replace(".", "", 1).isdigit() else min(2 ** attempt, 60)
                    print(f"[retry {attempt}] HTTP {status}; sleeping {delay:.0f}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                try:
                    body = json.loads(text)
                except ValueError:
                    raise FrappeError(status, None, [text[:500]], text) from None
                raise FrappeError(status, body.get("exc_type"), _unpack_server_messages(body), body) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    delay = min(2 ** attempt, 30)
                    print(f"[retry {attempt}] {e}; sleeping {delay}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                raise

    # ------------------------------------------------------------------ meta
    def versions(self) -> dict:
        if self._versions is None:
            self._versions = self._request("GET", "/api/method/frappe.utils.change_log.get_versions").get("message", {})
        return self._versions

    @property
    def major(self) -> int:
        v = self.versions().get("frappe", {}).get("version", "15")
        try:
            return int(str(v).split(".")[0])
        except ValueError:
            return 15

    def whoami(self) -> str:
        return self._request("GET", "/api/method/frappe.auth.get_logged_user").get("message")

    def time_zone(self) -> str:
        return self._request("GET", "/api/method/frappe.client.get_time_zone").get("message", {}).get("time_zone")

    # ------------------------------------------------------------------ reads
    def get_list(self, doctype: str, fields=None, filters=None, or_filters=None, order_by: str = "name asc",
                 limit_start: int = 0, limit_page_length: int = 20, group_by: str | None = None,
                 parent: str | None = None) -> list[dict]:
        params = {"fields": fields or ["name"], "filters": filters, "or_filters": or_filters, "order_by": order_by,
                  "limit_start": limit_start, "limit_page_length": limit_page_length, "group_by": group_by,
                  "parent": parent}
        return self._request("GET", f"/api/resource/{urllib.parse.quote(doctype)}", params=params).get("data", [])

    def iter_all(self, doctype: str, fields=None, filters=None, order_by: str = "name asc", page: int = DEFAULT_PAGE,
                 parent: str | None = None):
        start = 0
        while True:
            rows = self.get_list(doctype, fields=fields, filters=filters, order_by=order_by, limit_start=start,
                                 limit_page_length=page, parent=parent)
            yield from rows
            if len(rows) < page:
                break
            start += page

    def get_count(self, doctype: str, filters=None) -> int:
        return int(self._request("GET", "/api/method/frappe.client.get_count",
                                 params={"doctype": doctype, "filters": filters}).get("message", 0))

    def get_doc(self, doctype: str, name: str) -> dict:
        return self._request("GET", f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='/')}").get("data", {})

    def getdoc(self, doctype: str, name: str) -> dict:
        """Doc + docinfo (attachments, comments, versions, assignments, links) in one call."""
        return self._request("GET", "/api/method/frappe.desk.form.load.getdoc", params={"doctype": doctype, "name": name})

    def get_value(self, doctype: str, filters, fieldname) -> dict:
        return self._request("GET", "/api/method/frappe.client.get_value",
                             params={"doctype": doctype, "filters": filters, "fieldname": fieldname}).get("message") or {}

    def group_count(self, doctype: str, field: str, filters=None, min_count: int = 1) -> list[dict]:
        """GROUP BY field with counts, branching on the Frappe major version."""
        if self.major >= 16:
            fields = [field, {"COUNT": "name", "as": "n"}]
        else:
            fields = [field, "count(name) as n"]
        rows = self._request("GET", "/api/method/frappe.client.get_list",
                             params={"doctype": doctype, "fields": fields, "filters": filters, "group_by": field,
                                     "order_by": "n desc", "limit_page_length": 0}).get("message", [])
        return [r for r in rows if int(r.get("n", 0)) >= min_count]

    def linked_docs(self, doctype: str, name: str) -> dict:
        """Which documents link to this one (used to score merge survivors)."""
        linkinfo = self._request("GET", "/api/method/frappe.desk.form.linked_with.get_linked_doctypes",
                                 params={"doctype": doctype}).get("message", {})
        return self._request("GET", "/api/method/frappe.desk.form.linked_with.get_linked_docs",
                             params={"doctype": doctype, "name": name, "linkinfo": linkinfo}).get("message", {})

    def export_csv(self, doctype: str, out_path: str, fields=None, filters=None, order_by: str | None = None) -> str:
        """reportview.export_query — streams CSV with no page limit (needs Export permission)."""
        params = {"doctype": doctype, "file_format_type": "CSV", "fields": fields or ["name"], "filters": filters,
                  "order_by": order_by}
        self._request("GET", "/api/method/frappe.desk.reportview.export_query", params=params,
                      stream_to=pathlib.Path(out_path))
        return out_path

    def webhook_log(self, webhook: str | None = None, since: str | None = None, limit: int = 50) -> list[dict]:
        filters = []
        if webhook:
            filters.append(["webhook", "=", webhook])
        if since:
            filters.append(["creation", ">", since])
        return self.get_list("Webhook Request Log", fields=["name", "creation", "webhook", "reference_document", "url",
                                                            "response", "error"],
                             filters=filters, order_by="creation desc", limit_page_length=limit)

    # ------------------------------------------------------------------ writes (dry-run aware)
    def insert(self, doctype: str, doc: dict):
        doc = {k: v for k, v in doc.items() if k != "doctype"}
        r = self._request("POST", f"/api/resource/{urllib.parse.quote(doctype)}", json_body=doc, mutating=True)
        return r.get("data", r)

    def insert_many(self, docs: list[dict]):
        if len(docs) > 200:
            raise ValueError("insert_many accepts at most 200 docs per call (all-or-nothing); chunk it")
        r = self._request("POST", "/api/method/frappe.client.insert_many", form={"docs": docs}, mutating=True)
        return r.get("message", r)

    def update(self, doctype: str, name: str, values: dict):
        r = self._request("PUT", f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='/')}",
                          json_body=values, mutating=True)
        return r.get("data", r)

    def set_value(self, doctype: str, name: str, fieldname, value=None):
        form = {"doctype": doctype, "name": name, "fieldname": fieldname}
        if value is not None:
            form["value"] = value
        r = self._request("POST", "/api/method/frappe.client.set_value", form=form, mutating=True)
        return r.get("message", r)

    def delete(self, doctype: str, name: str):
        return self._request("DELETE", f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='/')}",
                             mutating=True)

    def cancel(self, doctype: str, name: str):
        r = self._request("POST", "/api/method/frappe.client.cancel", form={"doctype": doctype, "name": name}, mutating=True)
        return r.get("message", r)

    def submit(self, doctype: str, name: str):
        r = self._request("POST", f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='/')}",
                          form={"run_method": "submit"}, mutating=True)
        return r.get("message", r)

    def rename_doc(self, doctype: str, old_name: str, new_name: str, merge: bool = False):
        r = self._request("POST", "/api/method/frappe.client.rename_doc",
                          form={"doctype": doctype, "old_name": old_name, "new_name": new_name, "merge": int(merge)},
                          mutating=True)
        return r.get("message", r)

    def call(self, method: str, http: str = "POST", **kwargs):
        if http.upper() == "GET":
            return self._request("GET", f"/api/method/{method}", params=kwargs).get("message")
        r = self._request("POST", f"/api/method/{method}", form=kwargs, mutating=True)
        return r.get("message", r)

    def add_tags(self, tags: list[str], doctype: str, docs: list[str]):
        return self.call("frappe.desk.doctype.tag.tag.add_tags", tags=tags, dt=doctype, docs=docs)

    def upload_file(self, path: str, doctype: str | None = None, docname: str | None = None,
                    fieldname: str | None = None, private: bool = True):
        p = pathlib.Path(path)
        boundary = f"----frappe{uuid.uuid4().hex}"
        parts = []
        for k, v in {"is_private": int(private), "doctype": doctype or "", "docname": docname or "",
                     "fieldname": fieldname or ""}.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{p.name}\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n".encode() + p.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        r = self._request("POST", "/api/method/upload_file", raw_body=b"".join(parts),
                          content_type=f"multipart/form-data; boundary={boundary}", mutating=True)
        return r.get("message", r)

    def data_import(self, doctype: str, csv_path: str, import_type: str = "Insert New Records", submit: bool = False):
        """Create Data Import → attach CSV → start (background job). Webhooks do NOT fire during import."""
        di = self.insert("Data Import", {"reference_doctype": doctype, "import_type": import_type,
                                         "submit_after_import": int(submit), "mute_emails": 1})
        if di.get("dry_run"):
            return {"dry_run": True, "steps": ["insert Data Import", "upload_file", "update import_file",
                                               "form_start_import"], "doctype": doctype, "file": csv_path}
        f = self.upload_file(csv_path, doctype="Data Import", docname=di["name"], fieldname="import_file", private=True)
        di = self.update("Data Import", di["name"], {"import_file": f["file_url"]})
        warnings = di.get("template_warnings")
        if warnings and json.loads(warnings):
            raise FrappeError(417, "TemplateWarnings", [str(warnings)])
        started = self.call("frappe.core.doctype.data_import.data_import.form_start_import", data_import=di["name"])
        return {"name": di["name"], "started": started}

    def import_status(self, name: str):
        return self.call("frappe.core.doctype.data_import.data_import.get_import_status", "GET", data_import_name=name)

    # ------------------------------------------------------------------ higher-level helpers
    def snapshot(self, doctype: str, name: str, snapshot_dir: str) -> str:
        """Save doc + docinfo to <dir>/<doctype>/<name>.json before any irreversible change."""
        d = pathlib.Path(snapshot_dir) / doctype.replace("/", "_")
        d.mkdir(parents=True, exist_ok=True)
        payload = self.getdoc(doctype, name)
        out = d / (name.replace("/", "__") + ".json")
        out.write_text(json.dumps(payload, indent=1, default=str))
        return str(out)

    def merge(self, doctype: str, loser: str, survivor: str, snapshot_dir: str | None = None):
        """Snapshot both, then rename_doc(loser → survivor, merge=1)."""
        snaps = []
        if snapshot_dir:
            snaps = [self.snapshot(doctype, loser, snapshot_dir), self.snapshot(doctype, survivor, snapshot_dir)]
        result = self.rename_doc(doctype, loser, survivor, merge=True)
        return {"doctype": doctype, "merged": loser, "into": survivor, "snapshots": snaps, "result": result}


# ---------------------------------------------------------------------- CLI helpers
def _load_json_arg(s: str | None):
    if s is None:
        return None
    if s.startswith("@"):
        return json.loads(pathlib.Path(s[1:]).read_text())
    return json.loads(s)


def _fields_arg(s: str | None):
    if not s:
        return None
    s = s.strip()
    if s.startswith("["):
        return json.loads(s)
    return [f.strip() for f in s.split(",") if f.strip()]


def _emit(obj, out: str | None = None, jsonl: bool = False):
    if out:
        p = pathlib.Path(out)
        if jsonl or p.suffix == ".jsonl":
            with p.open("w") as fh:
                for row in obj:
                    fh.write(json.dumps(row, default=str) + "\n")
        else:
            p.write_text(json.dumps(obj, indent=1, default=str))
        print(f"wrote {out} ({len(obj) if hasattr(obj, '__len__') else 1} records)", file=sys.stderr)
    else:
        print(json.dumps(obj, indent=1, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL"))
    ap.add_argument("--key", default=os.environ.get("ERPNEXT_API_KEY"))
    ap.add_argument("--secret", default=os.environ.get("ERPNEXT_API_SECRET"))
    ap.add_argument("--apply", action="store_true", help="actually send mutating requests (default: dry-run)")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("versions")
    sub.add_parser("whoami")
    sub.add_parser("timezone")

    p = sub.add_parser("list"); p.add_argument("doctype"); p.add_argument("--fields"); p.add_argument("--filters")
    p.add_argument("--or-filters"); p.add_argument("--order-by", default="name asc"); p.add_argument("--start", type=int, default=0)
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE); p.add_argument("--all", action="store_true")
    p.add_argument("--parent"); p.add_argument("--out")

    p = sub.add_parser("count"); p.add_argument("doctype"); p.add_argument("--filters")
    p = sub.add_parser("get"); p.add_argument("doctype"); p.add_argument("name"); p.add_argument("--docinfo", action="store_true"); p.add_argument("--out")
    p = sub.add_parser("groupcount"); p.add_argument("doctype"); p.add_argument("field"); p.add_argument("--filters"); p.add_argument("--min", type=int, default=1); p.add_argument("--out")
    p = sub.add_parser("linked"); p.add_argument("doctype"); p.add_argument("name")
    p = sub.add_parser("export"); p.add_argument("doctype"); p.add_argument("--fields"); p.add_argument("--filters"); p.add_argument("--order-by"); p.add_argument("--csv", required=True)
    p = sub.add_parser("webhook-log"); p.add_argument("webhook", nargs="?"); p.add_argument("--since"); p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("insert"); p.add_argument("doctype"); p.add_argument("--json", required=True, help="JSON or @file")
    p = sub.add_parser("insert-many"); p.add_argument("--json", required=True, help="JSON list (each with doctype) or @file")
    p = sub.add_parser("update"); p.add_argument("doctype"); p.add_argument("name"); p.add_argument("--json", required=True)
    p = sub.add_parser("set-value"); p.add_argument("doctype"); p.add_argument("name"); p.add_argument("fieldname"); p.add_argument("value", nargs="?")
    p = sub.add_parser("delete"); p.add_argument("doctype"); p.add_argument("name"); p.add_argument("--snapshot-dir")
    p = sub.add_parser("cancel"); p.add_argument("doctype"); p.add_argument("name"); p.add_argument("--snapshot-dir")
    p = sub.add_parser("submit"); p.add_argument("doctype"); p.add_argument("name")
    p = sub.add_parser("merge"); p.add_argument("doctype"); p.add_argument("loser"); p.add_argument("survivor"); p.add_argument("--snapshot-dir")
    p = sub.add_parser("call"); p.add_argument("method"); p.add_argument("--args", help="JSON kwargs or @file"); p.add_argument("--get", action="store_true")
    p = sub.add_parser("tag"); p.add_argument("doctype"); p.add_argument("tag"); p.add_argument("docs", nargs="+")
    p = sub.add_parser("upload"); p.add_argument("path"); p.add_argument("--doctype"); p.add_argument("--docname"); p.add_argument("--fieldname"); p.add_argument("--public", action="store_true")
    p = sub.add_parser("data-import"); p.add_argument("doctype"); p.add_argument("csv"); p.add_argument("--update", action="store_true"); p.add_argument("--submit", action="store_true")
    p = sub.add_parser("import-status"); p.add_argument("name")
    p = sub.add_parser("snapshot"); p.add_argument("doctype"); p.add_argument("name"); p.add_argument("--snapshot-dir", default="snapshots")

    for sp in sub.choices.values():  # accept --apply before or after the subcommand
        sp.add_argument("--apply", dest="apply_sub", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.apply = a.apply or a.apply_sub
    fr = Frappe(a.url or "", a.key or "", a.secret or "", dry_run=not a.apply, timeout=a.timeout, verbose=a.verbose)

    try:
        if a.cmd == "versions":
            _emit(fr.versions())
        elif a.cmd == "whoami":
            _emit({"user": fr.whoami()})
        elif a.cmd == "timezone":
            _emit({"time_zone": fr.time_zone()})
        elif a.cmd == "list":
            fields = _fields_arg(a.fields); filters = _load_json_arg(a.filters); orf = _load_json_arg(a.or_filters)
            if a.all:
                rows = list(fr.iter_all(a.doctype, fields=fields, filters=filters, order_by=a.order_by, page=a.page_size, parent=a.parent))
            else:
                rows = fr.get_list(a.doctype, fields=fields, filters=filters, or_filters=orf, order_by=a.order_by,
                                   limit_start=a.start, limit_page_length=a.page_size, parent=a.parent)
            _emit(rows, a.out, jsonl=bool(a.out and a.out.endswith(".jsonl")))
        elif a.cmd == "count":
            _emit({"doctype": a.doctype, "count": fr.get_count(a.doctype, _load_json_arg(a.filters))})
        elif a.cmd == "get":
            _emit(fr.getdoc(a.doctype, a.name) if a.docinfo else fr.get_doc(a.doctype, a.name), a.out)
        elif a.cmd == "groupcount":
            _emit(fr.group_count(a.doctype, a.field, _load_json_arg(a.filters), a.min), a.out)
        elif a.cmd == "linked":
            _emit(fr.linked_docs(a.doctype, a.name))
        elif a.cmd == "export":
            _emit({"csv": fr.export_csv(a.doctype, a.csv, _fields_arg(a.fields), _load_json_arg(a.filters), a.order_by)})
        elif a.cmd == "webhook-log":
            _emit(fr.webhook_log(a.webhook, a.since, a.limit))
        elif a.cmd == "insert":
            _emit(fr.insert(a.doctype, _load_json_arg(a.json)))
        elif a.cmd == "insert-many":
            _emit(fr.insert_many(_load_json_arg(a.json)))
        elif a.cmd == "update":
            _emit(fr.update(a.doctype, a.name, _load_json_arg(a.json)))
        elif a.cmd == "set-value":
            fieldname = _load_json_arg(a.fieldname) if a.fieldname.strip().startswith("{") else a.fieldname
            _emit(fr.set_value(a.doctype, a.name, fieldname, a.value))
        elif a.cmd in ("delete", "cancel"):
            snap = fr.snapshot(a.doctype, a.name, a.snapshot_dir) if a.snapshot_dir else None
            res = fr.delete(a.doctype, a.name) if a.cmd == "delete" else fr.cancel(a.doctype, a.name)
            _emit({"snapshot": snap, "result": res})
        elif a.cmd == "submit":
            _emit(fr.submit(a.doctype, a.name))
        elif a.cmd == "merge":
            _emit(fr.merge(a.doctype, a.loser, a.survivor, a.snapshot_dir))
        elif a.cmd == "call":
            kwargs = _load_json_arg(a.args) or {}
            _emit(fr.call(a.method, "GET" if a.get else "POST", **kwargs))
        elif a.cmd == "tag":
            _emit(fr.add_tags([a.tag], a.doctype, a.docs))
        elif a.cmd == "upload":
            _emit(fr.upload_file(a.path, a.doctype, a.docname, a.fieldname, private=not a.public))
        elif a.cmd == "data-import":
            _emit(fr.data_import(a.doctype, a.csv, "Update Existing Records" if a.update else "Insert New Records", a.submit))
        elif a.cmd == "import-status":
            _emit(fr.import_status(a.name))
        elif a.cmd == "snapshot":
            _emit({"snapshot": fr.snapshot(a.doctype, a.name, a.snapshot_dir)})
    except FrappeError as e:
        print(f"error: {e}", file=sys.stderr)
        if a.verbose and e.raw:
            print(json.dumps(e.raw, indent=1)[:4000], file=sys.stderr)
        return 2
    if fr.dry_run and a.cmd in {"insert", "insert-many", "update", "set-value", "delete", "cancel", "submit", "merge",
                                "tag", "upload", "data-import"} or (a.cmd == "call" and not a.get and fr.dry_run):
        print("dry-run: nothing was sent. Re-run with --apply to execute.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
