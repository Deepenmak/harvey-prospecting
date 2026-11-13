#!/usr/bin/env python3
"""Push this workspace's private company configuration to a deployed Sellbird.

The repo is public, so the real positioning, pricing, objections, targeting
and product knowledge live in files Git never sees (``sellbird.local.yaml``,
``skills/product_knowledge.md``, ``skills/competitive_intel.md``). A
deployment therefore starts from the untrained template and has to be told
who it works for. The dashboard's "Your company" tab is where that is typed;
this script types it for you, over the same API, from the files you already
have here.

It only ever writes configuration. It spends nothing, sends no email, and
touches no contact, campaign or key.

Usage:
    python scripts/push_config.py --url https://sellbird.example.com
    python scripts/push_config.py --url ... --dry-run     # show, send nothing

Credentials: DASHBOARD_USER and DASHBOARD_PASS from the environment (the
same pair set on the server), else asked for at the prompt. The password is
never printed, logged or stored.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent

# What the dashboard's Your company tab accepts, section by section. Anything
# outside this list is reported at the end instead, to be put on the server
# by hand -- the API refuses unknown fields on purpose.
SECTIONS = {
    "persona": ("name", "role", "company", "tone", "email"),
    "product": ("name", "description", "pricing", "key_benefits",
                "objection_responses"),
    "icp": ("industries", "titles", "company_size", "geography"),
}
KNOWLEDGE = {
    "knowledge": "skills/product_knowledge.md",
    "competitors": "skills/competitive_intel.md",
}


def local_config() -> tuple[Path, dict]:
    for name in ("sellbird.local.yaml", "sellbird.yaml"):
        path = ROOT / name
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                sys.exit(f"  {name} is not a YAML mapping.")
            return path, data
    sys.exit("  No sellbird.local.yaml or sellbird.yaml in this folder.")


def payloads(config: dict) -> list[tuple[str, dict]]:
    out = []
    for section, fields in SECTIONS.items():
        block = config.get(section)
        if not isinstance(block, dict):
            continue
        chosen = {k: block[k] for k in fields if block.get(k) not in (None, "", [], {})}
        if chosen:
            out.append((section, chosen))
    offer = ((config.get("product") or {}).get("offer") or {}).get("entry")
    if isinstance(offer, str) and offer.strip():
        out.append(("offer", {"entry": offer.strip()}))
    return out


def leftovers(config: dict) -> dict:
    """The settings the Your company API cannot carry. They belong in the
    config file on the server; the script prints them ready to paste."""
    rest: dict = {}
    for section, fields in SECTIONS.items():
        block = config.get(section)
        if not isinstance(block, dict):
            continue
        extra = {k: v for k, v in block.items()
                 if k not in fields and k != "offer" and v not in (None, "", [], {})}
        if extra:
            rest[section] = extra
    for key in ("channels", "usage", "brain", "writer", "compliance", "handoff"):
        if config.get(key) not in (None, {}, []):
            rest[key] = config[key]
    return rest


def login(client: httpx.Client, base: str, user: str, password: str) -> str:
    r = client.post(f"{base}/api/auth/login",
                    json={"username": user, "password": password})
    if r.status_code == 401:
        sys.exit("  The dashboard refused that username and password.")
    if r.status_code == 503:
        sys.exit("  That deployment has no DASHBOARD_USER / DASHBOARD_PASS set yet.")
    r.raise_for_status()
    token = (r.json() or {}).get("token")
    if not token:
        sys.exit("  The dashboard did not return a token.")
    return token


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True,
                    help="The deployment's base URL, e.g. https://sellbird.example.com")
    ap.add_argument("--user", default=os.getenv("DASHBOARD_USER", ""),
                    help="Dashboard username (default: $DASHBOARD_USER)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be sent; send nothing")
    ap.add_argument("--skip-knowledge", action="store_true",
                    help="Config only: leave product_knowledge.md and competitive_intel.md alone")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    path, config = local_config()
    sections = payloads(config)
    files = {} if args.skip_knowledge else {
        kind: (ROOT / rel) for kind, rel in KNOWLEDGE.items() if (ROOT / rel).exists()}

    print(f"\n  From: {path.name}")
    for section, fields in sections:
        print(f"    {section:<9} {', '.join(sorted(fields))}")
    for kind, file in files.items():
        print(f"    {kind:<9} {file.name} ({file.stat().st_size:,} bytes)")

    rest = leftovers(config)
    if args.dry_run:
        print("\n  Dry run: nothing was sent.")
        if rest:
            print("\n  Not carried by the API (put these in the config file on the server):")
            print(yaml.safe_dump(rest, sort_keys=False, allow_unicode=True))
        return 0

    user = args.user or input("  Dashboard username: ").strip()
    password = os.getenv("DASHBOARD_PASS") or getpass.getpass("  Dashboard password: ")
    if not user or not password:
        sys.exit("  A username and password are needed.")

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        token = login(client, base, user, password)
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        print()
        for section, fields in sections:
            r = client.post(f"{base}/api/company", headers=headers,
                            json={"section": section, "fields": fields})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            ok = r.status_code == 200 and body.get("success", True)
            print(f"    {section:<9} {'saved' if ok else 'NOT saved: ' + str(body.get('message') or r.status_code)}")
        for kind, file in files.items():
            r = client.post(f"{base}/api/company/knowledge", headers=headers,
                            json={"kind": kind, "text": file.read_text(encoding="utf-8")})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            ok = r.status_code == 200 and body.get("success", True)
            print(f"    {kind:<9} {'saved' if ok else 'NOT saved: ' + str(body.get('message') or r.status_code)}")

    print("\n  Done. The deployment reads it on the next heartbeat.")
    if rest:
        print("\n  These settings the Your company tab doesn't cover (targeting beyond")
        print("  industries and titles, the mail provider, limits, quiet hours). Run this")
        print("  in the deployment's terminal to merge them into its config file:\n")
        print(server_patch(rest))
    return 0


def server_patch(rest: dict) -> str:
    """A paste-ready command that merges ``rest`` into the config file on the
    server, leaving everything else in it alone. The YAML travels base64 so
    no quoting in it can break the paste."""
    import base64
    blob = base64.b64encode(
        yaml.safe_dump(rest, sort_keys=False, allow_unicode=True).encode("utf-8")
    ).decode("ascii")
    return f"""python - <<'PATCH'
import base64, pathlib, yaml
path = pathlib.Path("/config/sellbird.local.yaml")
if not path.exists():
    path = pathlib.Path("/config/sellbird.yaml")
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {{}}
patch = yaml.safe_load(base64.b64decode("{blob}").decode("utf-8"))
def merge(into, new):
    for key, value in new.items():
        into[key] = (merge(into[key], value)
                     if isinstance(value, dict) and isinstance(into.get(key), dict)
                     else value)
    return into
path.with_suffix(path.suffix + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
path.write_text(yaml.safe_dump(merge(config, patch), sort_keys=False, allow_unicode=True), encoding="utf-8")
print("Patched " + str(path))
PATCH"""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as e:
        sys.exit(f"\n  Could not reach the dashboard: {type(e).__name__}: {e}\n")
    except json.JSONDecodeError:
        sys.exit("\n  The dashboard answered with something that isn't JSON.\n")
