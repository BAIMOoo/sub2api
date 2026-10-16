#!/usr/bin/env python3
"""Read-only verification of the migration's scoped Cloudflare credential."""

import argparse
import json
import pathlib
import re
import stat
import urllib.error
import urllib.parse
import urllib.request


def load_token(path):
    if path.is_symlink():
        raise ValueError("refusing symlink credential file")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("credential must be a private regular file (0600)")
    match = re.fullmatch(r"CLOUDFLARE_API_TOKEN='([A-Za-z0-9_-]+)'\n?", path.read_text())
    if not match:
        raise ValueError("expected a single CLOUDFLARE_API_TOKEN entry")
    return match[1]


def request(token, path, params=None):
    url = "https://api.cloudflare.com/client/v4/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Cloudflare returned HTTP {exc.code}; credential not displayed") from None
    if not data.get("success"):
        raise RuntimeError("Cloudflare rejected the read-only request; credential not displayed")
    return data["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=pathlib.Path,
                        default=pathlib.Path.home()/".config/sub2api-migration/cloudflare.env")
    args = parser.parse_args()
    token = load_token(args.env_file)
    verified = request(token, "user/tokens/verify")
    if verified.get("status") != "active":
        raise RuntimeError("Cloudflare credential is not active")
    zones = request(token, "zones", {"name": "b4im.com", "per_page": 50})
    if len(zones) != 1 or zones[0]["name"] != "b4im.com":
        raise RuntimeError("expected exactly one authorized b4im.com zone")
    zone = zones[0]
    records = request(token, "zones/"+zone["id"]+"/dns_records",
                      {"name": "api.b4im.com", "per_page": 100})
    keys = ("id", "name", "type", "content", "ttl", "proxied", "modified_on")
    print(json.dumps({"credential_active": True, "zone_id": zone["id"],
                      "zone": zone["name"], "records": [
                          {key: row.get(key) for key in keys} for row in records],
                      "dns_changed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
