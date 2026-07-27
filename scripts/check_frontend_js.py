#!/usr/bin/env python3
"""Syntax-check the inline JavaScript in the frontend HTML.

The app is a single large HTML file with inline <script> blocks, so a typo
there is invisible to Python tooling and only shows up as a blank page. This
extracts each inline block and runs `node --check` over it.
"""

import pathlib
import re
import subprocess
import sys
import tempfile

FILES = ["frontend/index.html"]
EXTRA = ["frontend/theme.js"]


def check(source: str, label: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(source)
        path = fh.name
    result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    ok = result.returncode == 0
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(result.stderr.strip())
    pathlib.Path(path).unlink(missing_ok=True)
    return ok


def main() -> int:
    failures = 0
    for name in FILES:
        html = pathlib.Path(name).read_text()
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
        if not blocks:
            print(f"  FAIL {name}: no inline script found")
            failures += 1
            continue
        for i, block in enumerate(blocks):
            if not block.strip():
                continue
            if not check(block, f"{name} inline block {i + 1} ({len(block)} chars)"):
                failures += 1
    for name in EXTRA:
        if not check(pathlib.Path(name).read_text(), name):
            failures += 1
    print("frontend JS: OK" if not failures else f"frontend JS: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
