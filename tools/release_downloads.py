#!/usr/bin/env python3
"""Print the download count of every GitHub release asset of this project.

GitHub does not show asset download counts in the web UI; they are only
exposed through the API. This script queries it via the `gh` CLI (which must
be installed and authenticated: https://cli.github.com).

Usage:
    python tools/release_downloads.py [owner/repo]

Defaults to this project's repository when no argument is given.
"""
import json
import subprocess
import sys

DEFAULT_REPO = "etovarluque/ONTbarcoder-3-IAvH-LGC"


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/releases"],
            capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        sys.exit("Error: the `gh` CLI is not installed or not on PATH "
                 "(https://cli.github.com).")
    if proc.returncode != 0:
        sys.exit(f"Error querying the GitHub API:\n{proc.stderr.strip()}")

    releases = json.loads(proc.stdout)
    if not releases:
        print(f"{repo}: no releases found.")
        return
    total = 0
    for r in releases:
        tag = r.get("tag_name", "?")
        name = r.get("name") or tag
        print(f"{tag} ({name}):")
        if not r.get("assets"):
            print("  (no assets)")
        for a in r.get("assets", []):
            n = a.get("download_count", 0)
            total += n
            size_mb = a.get("size", 0) / 1_048_576
            print(f"  {a.get('name', '?')}: {n} downloads ({size_mb:.1f} MB)")
    print(f"\nTotal downloads across all releases: {total}")


if __name__ == "__main__":
    main()
