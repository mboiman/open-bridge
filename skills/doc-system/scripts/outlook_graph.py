#!/usr/bin/env python3
"""
Outlook (Microsoft Graph) source for doc-system.
Generic — no PII. Account, tenant, client_id, token_cache come from
workflow/contexts/doc-system.yaml sources[kind=outlook_graph] +
identity/accounts/m365-<tenant>.yaml.

Usage (env-driven, set before each call):
    DOC_SYSTEM_OUTLOOK_TENANT=<tenant-uuid>
    DOC_SYSTEM_OUTLOOK_CLIENT_ID=<client-uuid>
    DOC_SYSTEM_OUTLOOK_TOKEN_CACHE=/path/to/token.txt
    DOC_SYSTEM_OUTLOOK_EXTRACT=~/Desktop/_Outlook_Attachments

Commands:
    list                                — geflaggte Mails mit Anhaengen
    extract <message_id>                — alle PDF-Anhaenge speichern
    unflag <message_id>                 — Flagge entfernen
    device-code                         — Device Code Flow starten (Token holen)

Token-Renewal: bei 401 manuell `device-code` ausfuehren.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TENANT_ID = os.environ.get("DOC_SYSTEM_OUTLOOK_TENANT")
CLIENT_ID = os.environ.get("DOC_SYSTEM_OUTLOOK_CLIENT_ID")
TOKEN_CACHE = Path(os.environ.get(
    "DOC_SYSTEM_OUTLOOK_TOKEN_CACHE",
    "/tmp/graph_token.txt",
)).expanduser()
EXTRACT_DIR = Path(os.environ.get(
    "DOC_SYSTEM_OUTLOOK_EXTRACT",
    "~/Desktop/_Outlook_Attachments",
)).expanduser()
GRAPH = "https://graph.microsoft.com/v1.0"


def require_env():
    missing = [k for k in ("DOC_SYSTEM_OUTLOOK_TENANT", "DOC_SYSTEM_OUTLOOK_CLIENT_ID") if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing env: {', '.join(missing)} — set from workflow/contexts/doc-system.yaml")


def get_token() -> str:
    require_env()
    if not TOKEN_CACHE.exists():
        sys.exit(f"no token at {TOKEN_CACHE} — run: {sys.argv[0]} device-code")
    return TOKEN_CACHE.read_text().strip()


def curl(method: str, url: str, body: str | None = None) -> dict:
    token = get_token()
    # Encode literal spaces — a space is never valid unencoded in a URL, and
    # curl passes it through as-is. Graph then answers a $filter with spaces
    # (e.g. "flag/flagStatus eq 'flagged' and hasAttachments eq true") with an
    # empty object and NO error, so `list` silently finds nothing. %20 fixes it.
    url = url.replace(" ", "%20")
    cmd = ["curl", "-s", "-X", method, "-H", f"Authorization: Bearer {token}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", body]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout) if out.stdout else {}
    except json.JSONDecodeError:
        return {"_raw": out.stdout, "_err": out.stderr}


def cmd_list():
    q = f"{GRAPH}/me/messages?$filter=flag/flagStatus eq 'flagged' and hasAttachments eq true&$select=id,subject,from,receivedDateTime&$top=50"
    resp = curl("GET", q)
    if "error" in resp:
        sys.exit(json.dumps(resp["error"], indent=2))
    for i, m in enumerate(resp.get("value", []), 1):
        frm = m.get("from", {}).get("emailAddress", {})
        print(f"{i}. {m['subject']}")
        print(f"   Von: {frm.get('name','?')} <{frm.get('address','?')}>")
        print(f"   ID:  {m['id']}")
        print(f"   Empfangen: {m.get('receivedDateTime','?')}")
        print()


def cmd_extract(message_id: str):
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    resp = curl("GET", f"{GRAPH}/me/messages/{message_id}/attachments")
    if "error" in resp:
        sys.exit(json.dumps(resp["error"], indent=2))
    saved = []
    for att in resp.get("value", []):
        if not att.get("name", "").lower().endswith(".pdf"):
            continue
        data = base64.b64decode(att["contentBytes"])
        target = EXTRACT_DIR / att["name"]
        target.write_bytes(data)
        saved.append(str(target))
        print(f"Saved: {target}")
    if not saved:
        print("No PDF attachments found.")


def cmd_unflag(message_id: str):
    resp = curl("PATCH", f"{GRAPH}/me/messages/{message_id}", '{"flag":{"flagStatus":"notFlagged"}}')
    if "error" in resp:
        sys.exit(json.dumps(resp["error"], indent=2))
    print(f"Unflagged: {message_id}")


def cmd_device_code():
    require_env()
    scope = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite offline_access"
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/devicecode"
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", url,
         "-H", "Content-Type: application/x-www-form-urlencoded",
         "-d", f"client_id={CLIENT_ID}&scope={scope}"],
        capture_output=True, text=True, timeout=30,
    )
    dc = json.loads(r.stdout)
    if "error" in dc:
        sys.exit(json.dumps(dc, indent=2))
    print(dc["message"])
    print(f"\nuser_code: {dc['user_code']}\n")
    print("Polling for token (Ctrl-C to abort)...")
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    while True:
        time.sleep(int(dc.get("interval", 5)))
        tr = subprocess.run(
            ["curl", "-s", "-X", "POST", token_url,
             "-H", "Content-Type: application/x-www-form-urlencoded",
             "-d", f"client_id={CLIENT_ID}&grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code={dc['device_code']}"],
            capture_output=True, text=True, timeout=30,
        )
        tj = json.loads(tr.stdout)
        if "access_token" in tj:
            TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE.write_text(tj["access_token"])
            print(f"Token gespeichert: {TOKEN_CACHE}")
            return
        if tj.get("error") == "authorization_pending":
            continue
        sys.exit(json.dumps(tj, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    e = sub.add_parser("extract"); e.add_argument("message_id")
    u = sub.add_parser("unflag"); u.add_argument("message_id")
    sub.add_parser("device-code")
    args = p.parse_args()
    {"list": cmd_list,
     "extract": lambda: cmd_extract(args.message_id),
     "unflag": lambda: cmd_unflag(args.message_id),
     "device-code": cmd_device_code}[args.cmd]()
