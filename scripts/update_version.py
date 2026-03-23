#!/usr/bin/env python3
"""
Pre-commit hook: update __version__ in datacenter_manager/_version.py
to major.minor.<gitshortsha> on every commit.

The major.minor is taken from the current version in the file;
only the sha segment is replaced.
"""

import re
import subprocess
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).parent.parent / "datacenter_manager" / "_version.py"


def git_short_sha() -> str:
    # On the very first commit HEAD doesn't exist yet; fall back to the index tree SHA.
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    # First commit: hash the staged tree so we still get a real SHA.
    tree = subprocess.run(
        ["git", "write-tree"], capture_output=True, text=True, check=True,
    )
    return tree.stdout.strip()[:7]


def current_major_minor(text: str) -> str:
    """Extract major.minor from a version string like '1.2.abc1234' or '1.2.0'."""
    m = re.search(r'__version__\s*=\s*["\'](\d+)\.(\d+)', text)
    if not m:
        return "0.1"
    return f"{m.group(1)}.{m.group(2)}"


def main() -> None:
    content = VERSION_FILE.read_text()
    major_minor = current_major_minor(content)
    sha = git_short_sha()
    new_version = f"{major_minor}.{sha}"

    new_content = re.sub(
        r'__version__\s*=\s*["\'][^"\']*["\']',
        f'__version__ = "{new_version}"',
        content,
    )

    if new_content == content:
        # Nothing changed (same sha, e.g. amend) — still exit 0
        sys.exit(0)

    VERSION_FILE.write_text(new_content)
    # Stage the updated file so it's included in the commit
    subprocess.run(["git", "add", str(VERSION_FILE)], check=True)
    print(f"[version] updated to {new_version}")


if __name__ == "__main__":
    main()
