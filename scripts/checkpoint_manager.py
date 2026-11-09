#!/usr/bin/env python3
"""Restore independently validated stage artifacts from recent workflow runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "output"
SUCCESS_STATES = {"completed", "complete", "success", "done", "valid", "planned"}

STAGES = (
    ("story", ("story/story.json", "story/story_plan.json")),
    ("narration", ("narration/narration.json", "narration/audio_jobs.json")),
    ("flow_music", ("music/flow_music_manifest.json",)),
    ("visuals", (
        "visuals/visual_jobs_manifest.json",
        "visuals/image_manifest.json",
        "visuals/image_generation_manifest.json",
    )),
    ("depth_maps", ("visuals/depth_maps/depth_manifest.json",)),
    ("photo_motion", ("photo_motion/photo_motion_manifest.json",)),
    ("sound_design", (
        "sound_design/sound_design_manifest.json",
        "sound_design/sfx_cues.json",
        "sound_design/sfx_timing_validation.json",
    )),
    ("captions", ("captions/captions.json",)),
    ("scene_renders", ("scene_renders/render_manifest.json",)),
    ("scenes", ("scenes/scenes.json",)),
    ("thumbnail", ("thumbnail/thumbnail_manifest.json",)),
    ("metadata", ("publish/youtube_metadata.json",)),
)


def log(*values: Any, **kwargs: Any) -> None:
    kwargs.setdefault("flush", True)
    print(*values, **kwargs)


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if not infos:
            raise ValueError("empty checkpoint archive")
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != "output"
                or any(part in {"", ".", ".."} for part in path.parts)
                or ":" in path.parts[0]
            ):
                raise ValueError(f"unsafe checkpoint path: {name}")
            if name in seen:
                raise ValueError(f"duplicate checkpoint path: {name}")
            seen.add(name)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"checkpoint symlink: {name}")
            total += info.file_size
            if total > 8 * 1024 * 1024 * 1024:
                raise ValueError("checkpoint exceeds 8 GiB")
        destination.mkdir(parents=True, exist_ok=True)
        for info in infos:
            relative = PurePosixPath(info.filename)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            with zf.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def validate_context(saved: Path, expected: dict) -> bool:
    context = read_json(saved / ".resume-context.json")
    if not context:
        return False
    for key in (
        "format", "parts_min", "parts_max",
        "scenes_min", "scenes_max",
    ):
        if context.get(key) != expected.get(key):
            return False
    if not isinstance(context.get("naming_key"), str) or not context["naming_key"].strip():
        return False
    actual = context.get("actual_parts")
    if (
        not isinstance(actual, int)
        or isinstance(actual, bool)
        or not expected["parts_min"] <= actual <= expected["parts_max"]
    ):
        return False
    return True


def valid_corrected_story(directory: Path) -> bool:
    checkpoint = read_json(directory / "story/story_checkpoint.json")
    corrected_path = directory / "story/corrected_complete_story.txt"
    if not checkpoint or not corrected_path.is_file():
        return False
    try:
        corrected = corrected_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return False
    proof = checkpoint.get("proofreading")
    preservation = (
        proof.get("source_preservation_verification", {})
        if isinstance(proof, dict) else {}
    )
    stored = str(checkpoint.get("corrected_complete_story") or "").strip()
    words = checkpoint.get("corrected_complete_story_word_count")
    return bool(
        checkpoint.get("status") == "corrected_story_ready"
        and re.fullmatch(r"[0-9a-f]{64}", str(checkpoint.get("source_fingerprint", "")))
        and corrected
        and re.sub(r"\s+", " ", corrected) == re.sub(r"\s+", " ", stored)
        and isinstance(words, int)
        and not isinstance(words, bool)
        and words == len(re.findall(r"\S+", corrected))
        and isinstance(proof, dict)
        and proof.get("complete_story_reviewed") is True
        and preservation.get("passed") is True
        and str(checkpoint.get("final_title") or "").strip()
    )


def validate_story_stage(directory: Path, expected: dict) -> bool:
    story_path = directory / "story/story.json"
    plan_path = directory / "story/story_plan.json"
    story = read_json(story_path)
    plan = read_json(plan_path)
    if story and plan:
        parts = story.get("parts")
        plan_parts = plan.get("parts")
        context = read_json(directory / ".resume-context.json") or {}
        count = len(parts) if isinstance(parts, list) else 0
        title = str(
            story.get("final_title")
            or story.get("generated_title")
            or story.get("story_title")
            or story.get("title")
            or ""
        ).strip()
        title_slug = re.sub(
            r"[\\s_-]+", "-",
            re.sub(r"[^\\w\\s-]", "", title.lower(), flags=re.UNICODE),
        ).strip("-")[:100]
        if (
            isinstance(plan_parts, list)
            and story.get("status") in SUCCESS_STATES
            and plan.get("status") in SUCCESS_STATES
            and str(story.get("format", "")).lower() == expected["format"]
            and count > 0
            and count == len(plan_parts)
            and count == context.get("actual_parts")
            and title_slug == context.get("naming_key")
        ):
            if (
                story.get("story_plan_fingerprint")
                and story.get("story_plan_fingerprint") == plan.get("source_fingerprint")
            ):
                return True
    return valid_corrected_story(directory)


def manifest_complete(path: Path, allowed: set[str] | None = None) -> bool:
    data = read_json(path)
    if not data:
        return False
    status = str(data.get("status", "")).strip().lower()
    if allowed is not None:
        return status in allowed
    return status in SUCCESS_STATES


def validate_stage(name: str, directory: Path, expected: dict) -> bool:
    if name == "story":
        return validate_story_stage(directory, expected)
    if name == "visuals":
        paths = [directory / item for item in STAGES[3][1]]
        docs = [read_json(path) for path in paths]
        if not all(docs):
            return False
        if not all(str(doc.get("status", "")).lower() in SUCCESS_STATES for doc in docs):
            return False
        source = docs[0].get("source_fingerprint")
        config = docs[0].get("config_signature")
        return bool(
            source and config
            and all(doc.get("source_fingerprint") == source for doc in docs)
            and all(doc.get("config_signature") == config for doc in docs)
        )
    for relative in next(files for stage, files in STAGES if stage == name):
        path = directory / relative
        if not manifest_complete(path):
            return False
    return True


def copy_stage(source: Path, output: Path, name: str) -> int:
    copied = 0

    if name == "visuals":
        src_root = source / "visuals"
        dst_root = output / "visuals"
        if not src_root.is_dir():
            return 0
        dst_root.mkdir(parents=True, exist_ok=True)
        for child in sorted(src_root.iterdir()):
            if child.name == "depth_maps" or child.is_symlink():
                continue
            target = dst_root / child.name
            if target.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, target, symlinks=False)
                copied += sum(1 for item in target.rglob("*") if item.is_file())
            elif child.is_file():
                shutil.copy2(child, target)
                copied += 1
        return copied

    if name == "depth_maps":
        src = source / "visuals" / "depth_maps"
        dst = output / "visuals" / "depth_maps"
        if not src.is_dir() or dst.exists():
            return 0
        shutil.copytree(src, dst, symlinks=False)
        return sum(1 for item in dst.rglob("*") if item.is_file())

    files = next(files for stage, files in STAGES if stage == name)
    roots = sorted({Path(item).parts[0] for item in files})
    if name == "sound_design":
        roots = ["sound_design", "sfx", "sound"]
    elif name == "thumbnail":
        roots = ["thumbnail", "thumbnail.jpg"]
    elif name == "metadata":
        roots = ["publish", "youtube_metadata.txt"]
    for root_name in roots:
        src = source / root_name
        dst = output / root_name
        if not src.exists() or dst.exists() or src.is_symlink():
            continue
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        elif src.is_dir():
            shutil.copytree(src, dst, symlinks=False)
            copied += sum(1 for item in dst.rglob("*") if item.is_file())
    return copied



def merge_progress_checkpoints(source: Path, output: Path) -> int:
    roots = (
        "checkpoints",
        "story/checkpoints",
        "narration/checkpoints",
        "visuals/checkpoints",
        "visuals/depth_maps",
        "photo_motion/checkpoints",
        "scenes",
    )
    copied = 0
    for root_name in roots:
        source_root = source / root_name
        if not source_root.is_dir():
            continue
        for candidate in sorted(source_root.rglob("*.json")):
            if candidate.is_symlink():
                continue
            data = read_json(candidate)
            if not isinstance(data, dict):
                continue
            has_stage_identity = any(
                data.get(key)
                for key in (
                    "config_signature",
                    "config_hash",
                    "source_fingerprint",
                    "source_signature",
                    "source_hash",
                    "input_fingerprint",
                    "input_hash",
                    "input_signature",
                    "generation_signature",
                    "cache_signature",
                    "manifest_signature",
                    "job_signature",
                    "signature",
                    "fingerprint",
                )
            )
            if not has_stage_identity:
                continue
            relative = candidate.relative_to(source)
            destination = output / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, destination)
            copied += 1
    return copied


def get_expected_context() -> dict:
    sys.path.insert(0, str(ROOT))
    from scripts.input_config import (
        load_and_validate,
        get_format,
        get_parts_range,
        get_scenes_range,
    )

    config = load_and_validate(ROOT / "Input/topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts_min, parts_max = get_parts_range(config)
    scenes_min, scenes_max = get_scenes_range(config)
    if mode not in {"short", "full"}:
        raise ValueError(f"invalid FORMAT: {mode}")
    return {
        "format": mode,
        "parts_min": int(parts_min),
        "parts_max": int(parts_max),
        "scenes_min": int(scenes_min),
        "scenes_max": int(scenes_max),
    }


def list_runs(repository: str, workflow: str, branch: str, limit: int) -> list[int]:
    command = [
        "gh", "run", "list",
        "--repo", repository,
        "--workflow", workflow,
        "--branch", branch,
        "--status", "completed",
        "--limit", str(limit),
        "--json", "databaseId,status",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        log("Checkpoint search unavailable; continuing without cross-run restore:", result.stderr.strip())
        return []
    try:
        records = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        log("Checkpoint run list was invalid; continuing without cross-run restore:", exc)
        return []
    return [
        int(row["databaseId"])
        for row in records
        if row.get("status") == "completed" and row.get("databaseId")
    ]


def download_run(repository: str, run_id: int, destination: Path) -> Path | None:
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        "gh", "run", "download", str(run_id),
        "--repo", repository,
        "--name", f"katha-photo-motion-checkpoint-{run_id}",
        "--dir", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        return None
    archive = destination / "checkpoint.zip"
    return archive if archive.is_file() and archive.stat().st_size > 0 else None


def restore(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    expected_input = args.input_hash.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_input):
        raise ValueError("input hash must be a 64-character SHA256")
    expected = get_expected_context()
    restored: dict[str, int] = {}
    examined = 0
    rejected: list[dict] = []
    progress_checkpoints = 0

    with tempfile.TemporaryDirectory(prefix="katha-checkpoint-") as temporary:
        temp = Path(temporary)
        run_ids = list_runs(args.repository, args.workflow, args.branch, args.limit)
        for run_id in run_ids:
            examined += 1
            run_dir = temp / str(run_id)
            archive = download_run(args.repository, run_id, run_dir / "download")
            if archive is None:
                rejected.append({"run_id": run_id, "reason": "checkpoint artifact unavailable"})
                continue
            extracted = run_dir / "extracted"
            try:
                safe_extract(archive, extracted)
            except Exception as exc:
                rejected.append({"run_id": run_id, "reason": f"unsafe or invalid archive: {exc}"})
                continue
            saved = extracted / "output"
            input_file = saved / ".resume-input.sha256"
            if not input_file.is_file():
                rejected.append({"run_id": run_id, "reason": "input fingerprint missing"})
                continue
            saved_input = input_file.read_text(encoding="ascii", errors="ignore").strip().lower()
            if saved_input != expected_input:
                rejected.append({"run_id": run_id, "reason": "input fingerprint mismatch"})
                continue
            if not validate_context(saved, expected):
                rejected.append({"run_id": run_id, "reason": "FORMAT/PARTS/SCENES/naming context mismatch"})
                continue

            progress_checkpoints += merge_progress_checkpoints(saved, output)
            accepted = []
            for stage, _ in STAGES:
                if stage in restored:
                    continue
                if validate_stage(stage, saved, expected):
                    count = copy_stage(saved, output, stage)
                    if count:
                        restored[stage] = run_id
                        accepted.append(stage)

            if accepted:
                log(f"Run {run_id}: restored stages: {', '.join(accepted)}")
            if len(restored) == len(STAGES):
                break

    report = {
        "status": "completed",
        "runs_examined": examined,
        "run_limit": args.limit,
        "restored_stages": restored,
        "restored_stage_count": len(restored),
        "progress_checkpoint_files_merged": progress_checkpoints,
        "rejected_candidates": rejected,
        "pipeline_hash_policy": "not used as a global restore gate; stage manifests validate reuse",
    }
    report_path = output / "checkpoint_restore_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("Checkpoint runs examined:", examined)
    log("Independently restored stages:", len(restored), "/", len(STAGES))
    log("Progress checkpoint files merged:", progress_checkpoints)
    log("Restore report:", report_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("restore")
    command.add_argument("--repository", required=True)
    command.add_argument("--workflow", default="generate-photo-motion-video.yml")
    command.add_argument("--branch", required=True)
    command.add_argument("--limit", type=int, default=30)
    command.add_argument("--input-hash", required=True)
    command.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    return args


if __name__ == "__main__":
    try:
        arguments = parse_args()
        if arguments.command == "restore":
            raise SystemExit(restore(arguments))
    except KeyboardInterrupt:
        log("Checkpoint restore cancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("Checkpoint restore failed:", f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
