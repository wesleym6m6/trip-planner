#!/usr/bin/env python3
"""Print a complete no-write candidate for an explicit public release manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trip_planner.public_release import PublicReleaseError, prepare_public_release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a complete no-write public release manifest."
    )
    parser.add_argument("slug", nargs="+")
    arguments = parser.parse_args(argv)
    try:
        manifest = prepare_public_release(
            arguments.slug,
            source_root=REPO_ROOT / "public",
            template_dir=REPO_ROOT / "template",
        )
    except PublicReleaseError as error:
        print(
            json.dumps(
                {"ok": False, "status": "rejected", "code": error.code},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, "manifest": manifest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
