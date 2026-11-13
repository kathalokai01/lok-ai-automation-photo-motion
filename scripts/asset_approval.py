#!/usr/bin/env python3
"""Hash-bound human review ledger for generated still-image assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from input_config import load_and_validate

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output" / "visuals" / "storyboard_manifest.json"
TARGET = ROOT / "output" / "visuals" / "asset_approval.json"
APPROVAL_SOURCE = ROOT / "Input" / "asset_approval.json"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read approval file {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise RuntimeError(f"Approval file must contain an object: {path}")

    return value


def load() -> dict:
    if not SOURCE.is_file():
        raise RuntimeError("Storyboard manifest missing")

    storyboard = read_json(SOURCE)
    if storyboard.get("status") != "completed":
        raise RuntimeError("Storyboard is not completed")

    entries = storyboard.get("images")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Storyboard has no image entries")

    # Runtime ledger is loaded first; committed Input approvals take precedence.
    prior_by_id = {}
    for path in (TARGET, APPROVAL_SOURCE):
        if not path.is_file():
            continue

        previous = read_json(path)
        previous_assets = previous.get("assets", [])
        if not isinstance(previous_assets, list):
            raise RuntimeError(f"Invalid assets list in {path}")

        for row in previous_assets:
            if isinstance(row, dict) and row.get("scene_id"):
                prior_by_id[str(row["scene_id"])] = row

    assets = []
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("Storyboard contains an invalid image record")

        scene_id = str(item.get("scene_id") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()

        if not scene_id or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(
                "Storyboard image is missing a valid scene ID or SHA256"
            )

        previous = prior_by_id.get(scene_id, {})
        same_asset = previous.get("sha256") == digest
        decision = previous.get("decision", "pending") if same_asset else "pending"

        if decision not in {"approved", "rejected", "pending"}:
            decision = "pending"

        assets.append({
            "scene_id": scene_id,
            "path": item.get("path"),
            "sha256": digest,
            "decision": decision,
            "review_note": previous.get("review_note", "") if same_asset else "",
        })

    reviewed = all(row["decision"] == "approved" for row in assets)

    return {
        "status": "reviewed" if reviewed else "review_required",
        "source_storyboard_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "assets": assets,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene")
    parser.add_argument("--decision", choices=("approved", "rejected"))
    parser.add_argument("--note", default="")
    parser.add_argument("--enforce-required", action="store_true")
    args = parser.parse_args()

    if bool(args.scene) != bool(args.decision):
        parser.error("--scene and --decision must be supplied together")

    document = load()

    if args.scene:
        matching = [
            row for row in document["assets"]
            if row["scene_id"] == args.scene
        ]
        if not matching:
            raise RuntimeError(f"Unknown scene ID: {args.scene}")

        matching[0]["decision"] = args.decision
        matching[0]["review_note"] = args.note
        document["status"] = (
            "reviewed"
            if all(row["decision"] == "approved" for row in document["assets"])
            else "review_required"
        )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    required = config.get("ASSET_APPROVAL_REQUIRED") is True

    print(
        "ASSET APPROVAL:",
        document["status"],
        "| assets:",
        len(document["assets"]),
        "| required:",
        required,
    )

    if args.enforce_required and required and document["status"] != "reviewed":
        raise RuntimeError(
            "ASSET_APPROVAL_REQUIRED=true but image assets are not all approved. "
            "Add hash-matched approvals to Input/asset_approval.json."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
