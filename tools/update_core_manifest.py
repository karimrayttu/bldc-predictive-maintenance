"""Rewrite tools/core_manifest.json, the acquisition core's integrity hashes.

    python tools/update_core_manifest.py            # show what changed
    python tools/update_core_manifest.py --write    # and save it
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tools" / "core_manifest.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    old = json.loads(io.open(MANIFEST, encoding="utf-8").read())
    new, changed = {}, []
    for rel in old:
        path = REPO / rel
        if not path.exists():
            print(f"missing: {rel}", file=sys.stderr)
            return 1
        new[rel] = digest(path)
        if new[rel] != old[rel]:
            changed.append(rel)

    for rel in old:
        mark = "changed" if rel in changed else "same"
        print(f"  {mark:<8} {rel}")

    if not changed:
        print("\nmanifest already matches the tree")
        return 0
    if "--write" not in sys.argv:
        print(f"\n{len(changed)} file(s) differ. Re-run with --write to accept.")
        return 1

    io.open(MANIFEST, "w", encoding="utf-8", newline="\n").write(
        json.dumps(new, indent=2) + "\n")
    print(f"\nwrote {MANIFEST.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
