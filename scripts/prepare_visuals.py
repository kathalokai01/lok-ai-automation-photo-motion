
#!/usr/bin/env python3
"""Compatibility entry point for the current photo-motion visual pipeline.

The single source of truth for visual job creation is generate_visuals.py.
This prevents the legacy dictionary-based manifest from overwriting the
current list-based image jobs and their prompt fingerprints.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_visuals.py"


def main() -> int:
    if not GENERATOR.is_file():
        print(
            f"ERROR: Required visual job generator is missing: {GENERATOR}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print("=" * 60, flush=True)
    print("KATHA LOK AI — VISUAL PREPARATION COMPATIBILITY ENTRY", flush=True)
    print("Delegating to scripts/generate_visuals.py", flush=True)
    print("Using one canonical visual_jobs.json schema.", flush=True)
    print("=" * 60, flush=True)

    # Execute the canonical script as __main__, preserving its normal
    # argument-free entry point and error reporting.
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        runpy.run_path(str(GENERATOR), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr, flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
