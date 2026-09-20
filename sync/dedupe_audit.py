#!/usr/bin/env python3
"""
dedupe_audit.py — find, plan and (only after approval) repair duplicates in ERPNext.

The model is deliberately general so it fits custom designs:

    document  --projections-->  normalized key vectors
    two documents sharing any key  ==>  edge in a collision graph
    connected components            ==>  duplicate clusters
    survivor score                  ==>  which record to keep
    policy (merge | delete | cancel_delete | review)  ==>  actions in a plan you can read and edit

Subcommands
    census  [--doctypes A,B,...] [--out census.json]      counts per DocType (live site)
    spec    DOCTYPE                                       print the default projection spec (edit → --spec)
    scan    DOCTYPE [--from-json dump.json|.jsonl] [--spec spec.json] [--filters JSON]
            [--link-counts] [--out plan.json]             build the plan (live or offline)
    apply   plan.json [--snapshot-dir dir] [--only merge,delete,cancel_delete] [--max N] [--apply]
            execute the plan; DRY-RUN unless --apply; snapshots every touched doc first

Offline dumps: a JSON list or JSONL of rows with at least "name" plus the spec's fields. Rows may carry
"_links": <int> (count of linked transactions) to inform survivor choice without a live site.

Everything here is stdlib. Credentials come from ERPNEXT_URL / ERPNEXT_API_KEY / ERPNEXT_API_SECRET.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import re
import sys
import unicodedata
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from frappe_client import Frappe, FrappeError  # noqa: E402

# ----------------------------------------------------------------------------- normalizers
LEGAL_SUFFIXES = r"\b(the|ltd|limited|llc|inc|incorporated|gmbh|ag|bv|nv|sa|sarl|srl|plc|pty|proprietary|co|corp|corporation|company|cc|kg|oy|ab|as|spa|sl|llp|lp|holdings?)\b"
ERPNEXT_SUFFIX = re.compile(r"\s*-\s*\d+$")  # ERPNext appends " - 1", " - 2" on name collisions


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def n_identity(v):
    return str(v).strip() if v not in (None, "") else None


def n_fold(v):
    """Case/accent/punctuation-insensitive company or person name, legal suffixes and ' - N' removed."""
    if v in (None, ""):
        return None
    s = _strip_accents(str(v)).lower()
    s = ERPNEXT_SUFFIX.sub("", s)
    s = re.sub(r"['’`]", "", s)          # sipho's → siphos
    s = re.sub(r"[&+]", " and ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(LEGAL_SUFFIXES, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def n_text(v):
    """Like fold but keeps legal words (for descriptions, remarks)."""
    if v in (None, ""):
        return None
    s = _strip_accents(str(v)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def n_email(v):
    if v in (None, ""):
        return None
    s = str(v).strip().lower()
    return s if "@" in s else None


def n_phone(v):
    """Digits only; keep the last 9 so +27 82…, 082… and 82… collide."""
    if v in (None, ""):
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) < 6:
        return None
    return d[-9:]


def n_alnum(v):
    if v in (None, ""):
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", str(v)).upper()
    return s or None


def n_amount(v):
    if v in (None, ""):
        return None
    try:
        return f"{round(float(v), 2):.2f}"
    except (TypeError, ValueError):
        return None


def n_date(v):
    return str(v)[:10] if v not in (None, "") else None


NORMALIZERS = {"identity": n_identity, "fold": n_fold, "text": n_text, "email": n_email, "phone": n_phone,
               "alnum": n_alnum, "amount": n_amount, "date": n_date}

# ----------------------------------------------------------------------------- default specs
# policy: merge (rename_doc merge=1) | delete (docstatus 0 only) | cancel_delete (submitted, status-gated) | review
COMMON = ["name", "creation", "modified", "owner", "docstatus"]

DEFAULT_SPECS: dict[str, dict] = {
    "Customer": {
        "fields": COMMON + ["customer_name", "customer_type", "customer_group", "territory", "tax_id", "email_id",
                            "mobile_no", "disabled", "customer_primary_contact", "customer_primary_address",
                            "default_currency", "represents_company"],
        "projections": [
            {"name": "name_fold", "keys": [["customer_name", "fold"]]},
            {"name": "tax_id", "keys": [["tax_id", "alnum"]]},
            {"name": "email", "keys": [["email_id", "email"]]},
            {"name": "phone", "keys": [["mobile_no", "phone"]]},
        ],
        "policy": "merge", "allow_rename": True,
        "link_counts": [["Sales Invoice", "customer"], ["Sales Order", "customer"], ["Delivery Note", "customer"],
                        ["Payment Entry", "party"], ["Quotation", "party_name"]],
        "merge_guards": ["same_or_empty:default_currency"],
    },
    "Supplier": {
        "fields": COMMON + ["supplier_name", "supplier_type", "supplier_group", "tax_id", "email_id", "mobile_no",
                            "disabled", "default_currency"],
        "projections": [
            {"name": "name_fold", "keys": [["supplier_name", "fold"]]},
            {"name": "tax_id", "keys": [["tax_id", "alnum"]]},
            {"name": "email", "keys": [["email_id", "email"]]},
            {"name": "phone", "keys": [["mobile_no", "phone"]]},
        ],
        "policy": "merge", "allow_rename": True,
        "link_counts": [["Purchase Invoice", "supplier"], ["Purchase Order", "supplier"], ["Payment Entry", "party"]],
        "merge_guards": ["same_or_empty:default_currency"],
    },
    "Contact": {
        "fields": COMMON + ["first_name", "last_name", "full_name", "email_id", "mobile_no", "phone", "company_name",
                            "status", "is_primary_contact"],
        "projections": [
            {"name": "email", "keys": [["email_id", "email"]]},
            {"name": "name_company", "keys": [["full_name", "fold"], ["company_name", "fold"]]},
            {"name": "phone", "keys": [["mobile_no", "phone"]]},
        ],
        "policy": "merge", "allow_rename": True,
        "link_counts": [["Sales Invoice", "contact_person"], ["Customer", "customer_primary_contact"]],
        "merge_guards": [],
        "note": "Copy missing email_ids/phone_nos/links rows onto the survivor before merging; the loser's child rows are deleted.",
    },
    "Address": {
        "fields": COMMON + ["address_title", "address_type", "address_line1", "address_line2", "city", "state",
                            "pincode", "country", "is_primary_address", "disabled"],
        "projections": [
            {"name": "street_city_pin", "keys": [["address_line1", "text"], ["city", "fold"], ["pincode", "alnum"]]},
            {"name": "street_city", "keys": [["address_line1", "text"], ["city", "fold"], ["country", "fold"]]},
        ],
        "policy": "merge", "allow_rename": True,
        "link_counts": [["Sales Invoice", "customer_address"], ["Customer", "customer_primary_address"]],
        "merge_guards": [],
    },
    "Item": {
        "fields": COMMON + ["item_code", "item_name", "item_group", "stock_uom", "is_stock_item", "has_serial_no",
                            "has_batch_no", "has_variants", "variant_of", "disabled", "description", "brand"],
        "projections": [
            {"name": "item_name_fold", "keys": [["item_name", "fold"]]},
            {"name": "code_fold", "keys": [["item_code", "alnum"]]},
        ],
        "policy": "merge", "allow_rename": True,
        "link_counts": [["Stock Ledger Entry", "item_code"], ["Item Price", "item_code"], ["BOM", "item"]],
        "merge_guards": ["same:stock_uom", "same:is_stock_item", "same:has_serial_no", "same:has_batch_no",
                         "same:has_variants"],
        "note": "ERPNext refuses to merge Items whose stock_uom/is_stock_item/has_serial_no/has_batch_no differ; stock is reposted for the survivor in the background.",
    },
    "Item Price": {
        "fields": COMMON + ["item_code", "price_list", "uom", "currency", "price_list_rate", "valid_from", "valid_upto",
                            "batch_no", "customer", "supplier", "selling", "buying"],
        "projections": [
            {"name": "exact", "keys": [["item_code", "identity"], ["price_list", "identity"], ["uom", "identity"],
                                       ["currency", "identity"], ["price_list_rate", "amount"], ["valid_from", "date"],
                                       ["valid_upto", "date"]], "allow_empty": ["uom", "valid_from", "valid_upto"]},
        ],
        "policy": "delete", "allow_rename": False, "link_counts": [], "merge_guards": [],
    },
    "Bank Transaction": {
        "fields": COMMON + ["date", "deposit", "withdrawal", "description", "bank_account", "reference_number",
                            "status", "currency", "unallocated_amount", "allocated_amount", "transaction_id"],
        "filters": [["docstatus", "!=", 2]],
        "projections": [
            {"name": "strict", "keys": [["bank_account", "identity"], ["date", "date"], ["deposit", "amount"],
                                        ["withdrawal", "amount"], ["description", "text"]]},
            {"name": "reference", "keys": [["bank_account", "identity"], ["reference_number", "alnum"],
                                           ["deposit", "amount"], ["withdrawal", "amount"]]},
            {"name": "transaction_id", "keys": [["bank_account", "identity"], ["transaction_id", "alnum"]]},
        ],
        "policy": "cancel_delete", "allow_rename": False, "link_counts": [], "merge_guards": [],
        "auto_ok_status": ["Unreconciled", "Pending"],
        "status_rank": {"Reconciled": 3, "Settled": 3, "Pending": 0, "Unreconciled": 0},
        "note": "Only Unreconciled/Pending duplicates are auto-actionable (cancel → delete). Reconciled/Settled rows are review-only: unreconcile by hand first.",
    },
    "Sales Invoice": {
        "fields": COMMON + ["customer", "posting_date", "grand_total", "status", "is_return", "return_against",
                            "amended_from", "outstanding_amount", "po_no"],
        "filters": [["docstatus", "!=", 2]],
        "projections": [
            {"name": "party_date_total", "keys": [["customer", "identity"], ["posting_date", "date"], ["grand_total", "amount"]]},
            {"name": "po_no", "keys": [["customer", "identity"], ["po_no", "alnum"]]},
        ],
        "policy": "review", "allow_rename": False, "link_counts": [], "merge_guards": [],
        "note": "Submitted invoices are never auto-actioned: confirm items match, then cancel + delete (or credit note).",
    },
    "Purchase Invoice": {
        "fields": COMMON + ["supplier", "posting_date", "grand_total", "status", "bill_no", "bill_date", "is_return",
                            "amended_from", "outstanding_amount"],
        "filters": [["docstatus", "!=", 2]],
        "projections": [
            {"name": "supplier_bill_no", "keys": [["supplier", "identity"], ["bill_no", "alnum"]]},
            {"name": "party_date_total", "keys": [["supplier", "identity"], ["posting_date", "date"], ["grand_total", "amount"]]},
        ],
        "policy": "review", "allow_rename": False, "link_counts": [], "merge_guards": [],
    },
    "Payment Entry": {
        "fields": COMMON + ["payment_type", "party_type", "party", "posting_date", "paid_amount", "received_amount",
                            "reference_no", "reference_date", "mode_of_payment", "status"],
        "filters": [["docstatus", "!=", 2]],
        "projections": [
            {"name": "party_date_amount", "keys": [["party", "identity"], ["posting_date", "date"], ["paid_amount", "amount"]]},
            {"name": "reference_amount", "keys": [["reference_no", "alnum"], ["paid_amount", "amount"]]},
        ],
        "policy": "review", "allow_rename": False, "link_counts": [], "merge_guards": [],
    },
    "Journal Entry": {
        "fields": COMMON + ["voucher_type", "posting_date", "total_debit", "cheque_no", "cheque_date", "user_remark",
                            "title"],
        "filters": [["docstatus", "!=", 2]],
        "projections": [
            {"name": "date_amount_remark", "keys": [["posting_date", "date"], ["total_debit", "amount"], ["user_remark", "text"]]},
            {"name": "cheque", "keys": [["cheque_no", "alnum"], ["total_debit", "amount"]]},
        ],
        "policy": "review", "allow_rename": False, "link_counts": [], "merge_guards": [],
    },
    "Lead": {
        "fields": COMMON + ["lead_name", "company_name", "email_id", "mobile_no", "status"],
        "projections": [
            {"name": "email", "keys": [["email_id", "email"]]},
            {"name": "name_company", "keys": [["lead_name", "fold"], ["company_name", "fold"]]},
            {"name": "phone", "keys": [["mobile_no", "phone"]]},
        ],
        "policy": "merge", "allow_rename": True, "link_counts": [["Opportunity", "party_name"], ["Customer", "lead_name"]],
        "merge_guards": [],
    },
}

CENSUS_DOCTYPES = ["Customer", "Supplier", "Contact", "Address", "Item", "Item Price", "Bank Transaction",
                   "Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry", "GL Entry", "Sales Order",
                   "Purchase Order", "Delivery Note", "Purchase Receipt", "Stock Entry", "Stock Ledger Entry", "Lead",
                   "Opportunity", "Quotation", "Employee", "User", "File", "Communication", "Error Log",
                   "Deleted Document", "Webhook Request Log", "Data Import"]


# ----------------------------------------------------------------------------- core algebra
class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def project(row: dict, projection: dict):
    """Return the normalized key tuple for one projection, or None if any required component is empty."""
    allow_empty = set(projection.get("allow_empty", []))
    key = []
    for field, norm in projection["keys"]:
        val = NORMALIZERS[norm](row.get(field))
        if val is None:
            if field in allow_empty:
                val = ""
            else:
                return None
        key.append(val)
    return tuple(key)


def build_clusters(rows: list[dict], spec: dict):
    """Collision graph → connected components. Returns (clusters, collisions_by_cluster)."""
    uf = UnionFind()
    buckets: dict[tuple[str, tuple], list[str]] = defaultdict(list)
    for r in rows:
        for pr in spec["projections"]:
            k = project(r, pr)
            if k is not None:
                buckets[(pr["name"], k)].append(r["name"])
    collisions = []
    for (pname, key), names in buckets.items():
        if len(names) > 1:
            collisions.append({"projection": pname, "key": list(key), "members": sorted(set(names))})
            first = names[0]
            for n in names[1:]:
                uf.union(first, n)
    groups: dict[str, list[str]] = defaultdict(list)
    for c in collisions:
        for n in c["members"]:
            groups[uf.find(n)].append(n)
    clusters = [sorted(set(v)) for v in groups.values() if len(set(v)) > 1]
    clusters.sort(key=lambda c: (-len(c), c[0]))
    coll_by_cluster: dict[str, list] = defaultdict(list)
    for c in collisions:
        coll_by_cluster[uf.find(c["members"][0])].append(c)
    return clusters, coll_by_cluster, uf


def _to_epoch(v) -> float:
    if not v:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(v)[:26]).timestamp()
    except ValueError:
        return 0.0


def survivor_score(row: dict, spec: dict) -> tuple:
    """Higher wins: linked transactions ≫ submitted > status rank > not disabled > clean name > older > completeness."""
    links = int(row.get("_links") or 0)
    submitted = 1 if row.get("docstatus") == 1 else 0
    status_rank = (spec.get("status_rank") or {}).get(row.get("status"), 0)
    not_disabled = 0 if row.get("disabled") else 1
    completeness = sum(1 for k, v in row.items() if not k.startswith("_") and v not in (None, "", 0))
    clean_name = 0 if ERPNEXT_SUFFIX.search(str(row.get("name", ""))) else 1
    age = -_to_epoch(row.get("creation"))  # earlier creation → larger score
    return (links, submitted, status_rank, not_disabled, clean_name, age, completeness)


def guard_ok(guards: list[str], a: dict, b: dict) -> tuple[bool, str | None]:
    for g in guards:
        kind, _, field = g.partition(":")
        va, vb = a.get(field), b.get(field)
        if kind == "same" and va != vb:
            return False, f"{field} differs ({va!r} vs {vb!r})"
        if kind == "same_or_empty" and va not in (None, "") and vb not in (None, "") and va != vb:
            return False, f"{field} differs ({va!r} vs {vb!r})"
    return True, None


def plan_actions(cluster: list[str], by_name: dict[str, dict], spec: dict, doctype: str) -> tuple[str, list[dict]]:
    members = [by_name[n] for n in cluster]
    members.sort(key=lambda r: survivor_score(r, spec), reverse=True)
    survivor = members[0]
    actions = []
    policy = spec.get("policy", "review")
    for loser in members[1:]:
        base = {"doctype": doctype, "loser": loser["name"], "survivor": survivor["name"]}
        if policy == "merge":
            ok, why = guard_ok(spec.get("merge_guards", []), survivor, loser)
            if not spec.get("allow_rename", True):
                actions.append({**base, "type": "review", "note": "DocType does not allow rename/merge"})
            elif not ok:
                actions.append({**base, "type": "review", "note": f"merge guard: {why}"})
            elif loser.get("docstatus") == 1:
                actions.append({**base, "type": "review", "note": "submitted document; merge not applicable"})
            else:
                actions.append({**base, "type": "merge",
                                "note": "rename_doc(loser → survivor, merge=1); copy wanted child rows first"})
        elif policy == "delete":
            if loser.get("docstatus", 0) == 0:
                actions.append({**base, "type": "delete", "note": "exact duplicate, draft/non-submittable"})
            else:
                actions.append({**base, "type": "review", "note": "not a draft"})
        elif policy == "cancel_delete":
            ds = loser.get("docstatus", 0)
            status = loser.get("status")
            auto_ok = spec.get("auto_ok_status", [])
            if ds == 0:
                actions.append({**base, "type": "delete", "note": "draft duplicate"})
            elif ds == 1 and (not auto_ok or status in auto_ok):
                actions.append({**base, "type": "cancel_delete", "note": f"submitted, status={status}: cancel then delete"})
            else:
                actions.append({**base, "type": "review", "note": f"status={status}: needs human (unreconcile/reverse first)"})
        else:
            actions.append({**base, "type": "review", "note": spec.get("note", "review manually")})
    return survivor["name"], actions


# ----------------------------------------------------------------------------- IO
def load_rows_from_file(path: str) -> list[dict]:
    p = pathlib.Path(path)
    text = p.read_text()
    if p.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        rows = data.get("data", data) if isinstance(data, dict) else data
    for r in rows:
        if "name" not in r:
            raise SystemExit("every row needs a 'name'")
    return rows


def fetch_rows_live(fr: Frappe, doctype: str, spec: dict, extra_filters) -> list[dict]:
    filters = list(spec.get("filters", []))
    if extra_filters:
        filters.extend(extra_filters if isinstance(extra_filters, list) else [extra_filters])
    return list(fr.iter_all(doctype, fields=spec["fields"], filters=filters or None, order_by="creation asc, name asc"))


def add_link_counts(fr: Frappe, spec: dict, rows: list[dict]):
    """Only for cluster members: count linking transactions (docstatus != 2)."""
    pairs = spec.get("link_counts") or []
    for r in rows:
        total = 0
        for dt_, field in pairs:
            try:
                total += fr.get_count(dt_, [[field, "=", r["name"]], ["docstatus", "!=", 2]])
            except FrappeError as e:
                if e.status in (403, 404, 417):
                    try:
                        total += fr.get_count(dt_, [[field, "=", r["name"]]])
                    except FrappeError:
                        pass
                else:
                    raise
        r["_links"] = total


def cmd_census(fr: Frappe, doctypes: list[str], out: str | None):
    rows = []
    for d in doctypes:
        try:
            total = fr.get_count(d)
            cancelled = fr.get_count(d, [["docstatus", "=", 2]]) if d not in ("User", "File", "Error Log") else 0
        except FrappeError as e:
            rows.append({"doctype": d, "error": str(e)})
            continue
        rows.append({"doctype": d, "count": total, "cancelled": cancelled})
    rows.sort(key=lambda r: -r.get("count", -1))
    result = {"site": fr.url, "user": fr.whoami(), "frappe": fr.versions().get("frappe", {}).get("version"),
              "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "doctypes": rows}
    _write(result, out)


def cmd_scan(fr: Frappe | None, doctype: str, spec: dict, from_json: str | None, extra_filters, link_counts: bool,
             out: str | None):
    if from_json:
        rows = load_rows_from_file(from_json)
        source = f"file:{from_json}"
    else:
        if fr is None:
            raise SystemExit("live scan needs ERPNEXT_* env vars (or use --from-json)")
        rows = fetch_rows_live(fr, doctype, spec, extra_filters)
        source = fr.url
    by_name = {r["name"]: r for r in rows}
    clusters, coll_by_cluster, uf = build_clusters(rows, spec)
    if link_counts and fr is not None and spec.get("link_counts"):
        add_link_counts(fr, spec, [by_name[n] for c in clusters for n in c])

    plan_clusters = []
    action_counts: dict[str, int] = defaultdict(int)
    for i, cluster in enumerate(clusters, 1):
        survivor, actions = plan_actions(cluster, by_name, spec, doctype)
        for a in actions:
            action_counts[a["type"]] += 1
        members = []
        for n in cluster:
            r = by_name[n]
            m = {k: v for k, v in r.items() if k in spec["fields"] or k.startswith("_")}
            m["_score"] = [str(x) for x in survivor_score(r, spec)]
            members.append(m)
        plan_clusters.append({"id": i, "size": len(cluster), "survivor": survivor,
                              "collisions": coll_by_cluster.get(uf.find(cluster[0]), []),
                              "members": members, "actions": actions})

    plan = {"doctype": doctype, "source": source,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "spec": spec,
            "stats": {"documents": len(rows), "clusters": len(clusters),
                      "documents_in_clusters": sum(len(c) for c in clusters), "actions": dict(action_counts)},
            "clusters": plan_clusters}
    _write(plan, out)
    s = plan["stats"]
    print(f"{doctype}: {s['documents']} docs → {s['clusters']} clusters ({s['documents_in_clusters']} docs); "
          f"actions {dict(action_counts)}", file=sys.stderr)


def cmd_apply(fr: Frappe, plan_path: str, snapshot_dir: str | None, only: set[str] | None, max_actions: int | None):
    plan = json.loads(pathlib.Path(plan_path).read_text())
    doctype = plan["doctype"]
    results = []
    done = 0
    for cluster in plan["clusters"]:
        for a in cluster["actions"]:
            if a["type"] == "review" or (only and a["type"] not in only):
                results.append({**a, "status": "skipped"})
                continue
            if max_actions is not None and done >= max_actions:
                results.append({**a, "status": "deferred (max reached)"})
                continue
            entry = {**a, "snapshots": []}
            try:
                if snapshot_dir and not fr.dry_run:
                    entry["snapshots"].append(fr.snapshot(doctype, a["loser"], snapshot_dir))
                    if a["type"] == "merge":
                        entry["snapshots"].append(fr.snapshot(doctype, a["survivor"], snapshot_dir))
                if a["type"] == "merge":
                    entry["result"] = fr.rename_doc(doctype, a["loser"], a["survivor"], merge=True)
                elif a["type"] == "delete":
                    entry["result"] = fr.delete(doctype, a["loser"])
                elif a["type"] == "cancel_delete":
                    entry["result"] = {"cancel": fr.cancel(doctype, a["loser"]), "delete": fr.delete(doctype, a["loser"])}
                entry["status"] = "dry-run" if fr.dry_run else "ok"
                done += 1
            except FrappeError as e:
                entry["status"] = "error"
                entry["error"] = str(e)
            results.append(entry)
    out = pathlib.Path(plan_path).with_suffix(".results.json")
    out.write_text(json.dumps({"doctype": doctype, "dry_run": fr.dry_run,
                               "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "results": results}, indent=1, default=str))
    tally = defaultdict(int)
    for r in results:
        tally[r["status"]] += 1
    print(f"{'DRY-RUN' if fr.dry_run else 'APPLIED'} {doctype}: {dict(tally)} → {out}", file=sys.stderr)


def _write(obj, out: str | None):
    if out:
        pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out).write_text(json.dumps(obj, indent=1, default=str))
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(json.dumps(obj, indent=1, default=str))


def load_spec(doctype: str, spec_path: str | None) -> dict:
    spec = copy.deepcopy(DEFAULT_SPECS.get(doctype)) or {"fields": COMMON, "projections": [], "policy": "review"}
    if spec_path:
        custom = json.loads(pathlib.Path(spec_path).read_text())
        spec.update(custom)
    if not spec.get("projections"):
        raise SystemExit(f"no projections for {doctype}: pass --spec with at least one projection "
                         f"(see `dedupe_audit.py spec Customer` for the shape)")
    for pr in spec["projections"]:
        for field, norm in pr["keys"]:
            if norm not in NORMALIZERS:
                raise SystemExit(f"unknown normalizer {norm!r}; choose from {sorted(NORMALIZERS)}")
            if field not in spec["fields"]:
                spec["fields"].append(field)
    return spec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="send mutating requests (apply only); default dry-run")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("census"); p.add_argument("--doctypes"); p.add_argument("--out")
    p = sub.add_parser("spec"); p.add_argument("doctype")
    p = sub.add_parser("scan"); p.add_argument("doctype"); p.add_argument("--from-json"); p.add_argument("--spec")
    p.add_argument("--filters", help="extra JSON filters (live only)"); p.add_argument("--link-counts", action="store_true")
    p.add_argument("--out")
    p = sub.add_parser("apply"); p.add_argument("plan"); p.add_argument("--snapshot-dir"); p.add_argument("--only")
    p.add_argument("--max", type=int)
    for sp in sub.choices.values():  # accept --apply before or after the subcommand
        sp.add_argument("--apply", dest="apply_sub", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.apply = a.apply or a.apply_sub

    have_env = all(k in __import__("os").environ for k in ("ERPNEXT_URL", "ERPNEXT_API_KEY", "ERPNEXT_API_SECRET"))
    fr = Frappe.from_env(dry_run=not a.apply, verbose=a.verbose) if have_env else None

    try:
        if a.cmd == "census":
            if fr is None:
                raise SystemExit("census needs ERPNEXT_* env vars")
            cmd_census(fr, [d.strip() for d in a.doctypes.split(",")] if a.doctypes else CENSUS_DOCTYPES, a.out)
        elif a.cmd == "spec":
            print(json.dumps(load_spec(a.doctype, None), indent=1))
        elif a.cmd == "scan":
            spec = load_spec(a.doctype, a.spec)
            filters = json.loads(a.filters) if a.filters else None
            cmd_scan(fr, a.doctype, spec, a.from_json, filters, a.link_counts, a.out)
        elif a.cmd == "apply":
            if fr is None:
                raise SystemExit("apply needs ERPNEXT_* env vars")
            only = {s.strip() for s in a.only.split(",")} if a.only else None
            cmd_apply(fr, a.plan, a.snapshot_dir, only, a.max)
            if fr.dry_run:
                print("dry-run: nothing was sent. Review the .results.json, then re-run with --apply.", file=sys.stderr)
    except FrappeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
