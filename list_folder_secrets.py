#!/usr/bin/env python3
"""List all secrets in a Delinea Secret Server folder."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_token_password(base_url: str, username: str, password: str) -> str:
    url = f"{base_url.rstrip('/')}/oauth2/token"
    resp = requests.post(
        url,
        data={"grant_type": "password", "username": username, "password": password},
        timeout=30,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        raise SystemExit(f"Authentication failed [{resp.status_code}]: {resp.text.strip()}")
    token = resp.json().get("access_token")
    if not token:
        raise SystemExit(f"No access_token in response: {resp.text.strip()}")
    return token


def _get_token_domain(base_url: str, username: str, password: str, domain: str) -> str:
    url = f"{base_url.rstrip('/')}/oauth2/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "domain": domain,
        },
        timeout=30,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        raise SystemExit(f"Authentication failed [{resp.status_code}]: {resp.text.strip()}")
    token = resp.json().get("access_token")
    if not token:
        raise SystemExit(f"No access_token in response: {resp.text.strip()}")
    return token


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_get(base_url: str, token: str, path: str, params: Optional[Dict] = None) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        raise SystemExit(
            f"API error [{resp.status_code}] {path}: {resp.text.strip()}"
        )
    return resp.json()


def list_folder_secrets(base_url: str, token: str, folder_id: int) -> List[Dict]:
    all_records: List[Dict] = []
    skip = 0
    while True:
        data = _api_get(base_url, token, "/api/v1/secrets", {
            "filter.folderId": folder_id,
            "filter.includeSubFolders": False,
            "take": _PAGE_SIZE,
            "skip": skip,
        })
        page = data.get("records", [])
        all_records.extend(page)
        if len(all_records) >= data.get("total", len(all_records)) or not page:
            break
        skip += _PAGE_SIZE
    return all_records


def get_secret_detail(base_url: str, token: str, secret_id: int) -> Dict:
    return _api_get(base_url, token, f"/api/v1/secrets/{secret_id}")


def _field_value(secret: Dict, slug: str) -> Optional[str]:
    for item in secret.get("items", []):
        if (item.get("slug") or "").lower() == slug.lower() or \
           (item.get("fieldName") or "").lower() == slug.lower():
            return item.get("itemValue") or item.get("value")
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in {None, ""} else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List all secrets in a Secret Server folder."
    )
    parser.add_argument("--base-url", default=_env("TSS_BASE_URL"),
                        help="Secret Server base URL (env: TSS_BASE_URL)")
    parser.add_argument("--username", default=_env("TSS_USERNAME"),
                        help="Username (env: TSS_USERNAME)")
    parser.add_argument("--password", default=_env("TSS_PASSWORD"),
                        help="Password (env: TSS_PASSWORD)")
    parser.add_argument("--domain", default=_env("TSS_DOMAIN"),
                        help="AD domain for domain logins (env: TSS_DOMAIN)")
    parser.add_argument("--folder-id", type=int, default=617,
                        help="Folder ID to enumerate (default: 617)")
    parser.add_argument("--details", action="store_true",
                        help="Fetch full secret details for each entry (slower)")
    parser.add_argument("--field", default=None,
                        help="With --details: print only this field slug per secret "
                             "(e.g. machine, username, password)")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print JSON output")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.base_url:
        parser.error("--base-url or TSS_BASE_URL is required")
    if not args.username or not args.password:
        parser.error("--username and --password are required")

    base_url = args.base_url.rstrip("/")

    if args.domain:
        token = _get_token_domain(base_url, args.username, args.password, args.domain)
    else:
        token = _get_token_password(base_url, args.username, args.password)

    secrets = list_folder_secrets(base_url, token, args.folder_id)
    print(f"# Found {len(secrets)} secret(s) in folder {args.folder_id}", file=sys.stderr)

    if not args.details:
        output = secrets
        print(json.dumps(output, indent=2 if args.pretty else None))
        return

    results = []
    for s in secrets:
        sid = s.get("id")
        detail = get_secret_detail(base_url, token, sid)
        if args.field:
            value = _field_value(detail, args.field)
            results.append({"id": sid, "name": s.get("name"), args.field: value})
        else:
            results.append(detail)

    print(json.dumps(results, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
