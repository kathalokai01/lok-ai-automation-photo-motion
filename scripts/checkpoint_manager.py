#!/usr/bin/env python3
"""Restore independently validated stage artifacts from recent workflow runs."""

from __future__ import annotations

import argparse
import hashlib
import struct
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
    ("story", ("story/story.json", "story/story_plan.json", "story/story_model_progression.json")),
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



def _story_fingerprint(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def valid_ai_story(directory: Path) -> bool:
    """Reject cached AI Story output unless its source and quality proofs match."""
    path = directory / "story/ai_story.json"
    if not path.is_file():
        return True
    ai_story = read_json(path)
    story = read_json(directory / "story/story.json")
    plan = read_json(directory / "story/story_plan.json")
    if not ai_story or not story or not plan:
        return False
    if ai_story.get("status") != "completed":
        return False
    if ai_story.get("source_story_signature") != _story_fingerprint(story):
        return False
    if ai_story.get("source_plan_signature") != _story_fingerprint(plan):
        return False
    report = ai_story.get("quality_validation")
    if not isinstance(report, dict) or report.get("passed") is not True:
        return False
    if report.get("status") != "passed":
        return False
    try:
        if float(report.get("score")) < 8:
            return False
    except (TypeError, ValueError):
        return False
    parts = ai_story.get("parts")
    quality_parts = report.get("parts")
    if not isinstance(parts, list) or not isinstance(quality_parts, list):
        return False
    if not parts or len(parts) != len(quality_parts):
        return False
    if any(
        not isinstance(part, dict)
        or part.get("status") != "completed"
        or not isinstance(part.get("quality_validation"), dict)
        or part["quality_validation"].get("passed") is not True
        for part in parts
    ):
        return False
    return True


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
                and valid_ai_story(directory)
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


def _checkpoint_output_file(directory: Path, value: Any) -> Path | None:
    """Resolve a manifest path inside an extracted checkpoint output tree."""
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip().replace("\\", "/")
    candidate = PurePosixPath(raw)

    if candidate.is_absolute():
        parts = candidate.parts
        try:
            output_index = parts.index("output")
        except ValueError:
            return None
        parts = parts[output_index + 1:]
    else:
        parts = candidate.parts
        if parts and parts[0] == "output":
            parts = parts[1:]

    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None

    root = directory.resolve()
    target = directory.joinpath(*parts)
    try:
        resolved = target.resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, RuntimeError):
        return None

    if target.is_symlink() or not resolved.is_file() or resolved.stat().st_size <= 0:
        return None
    return resolved


def validate_scene_render_stage(directory: Path) -> bool:
    """Restore a render manifest only when every referenced output is present."""
    manifest_path = directory / "scene_renders" / "render_manifest.json"
    manifest = read_json(manifest_path)
    if not manifest or str(manifest.get("status", "")).strip().lower() not in SUCCESS_STATES:
        return False

    path_keys = {
        "path", "file", "output", "output_path", "image", "image_path",
        "audio", "audio_path", "video", "video_path", "depth_path",
        "depth_map", "rendered_video", "source_path",
    }
    hash_keys = (
        "sha256", "image_sha256", "audio_sha256", "video_sha256",
        "depth_sha256", "file_sha256", "source_sha256",
    )
    checked = 0
    invalid = False

    def visit(value: Any) -> None:
        nonlocal checked, invalid
        if invalid:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in path_keys and isinstance(item, str) and item.strip():
                    asset = _checkpoint_output_file(directory, item)
                    if asset is None:
                        invalid = True
                        return
                    checked += 1
                    declared = next(
                        (
                            value.get(hash_key)
                            for hash_key in hash_keys
                            if isinstance(value.get(hash_key), str)
                            and value.get(hash_key).strip()
                        ),
                        None,
                    )
                    if declared and _sha256_file(asset).lower() != declared.strip().lower():
                        invalid = True
                        return
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(manifest)
    return not invalid and checked > 0


def validate_stage(name: str, directory: Path, expected: dict) -> bool:
    if name == "story":
        return validate_story_stage(directory, expected)
    if name == "scene_renders":
        return validate_scene_render_stage(directory)
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


def copy_tree_merge(source: Path, destination: Path) -> int:
    """Merge a verified checkpoint tree even when progress restore created its root."""
    if not source.is_dir() or source.is_symlink():
        return 0
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"Checkpoint destination is not a directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise RuntimeError(f"Checkpoint contains a symlink: {item}")
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    return sum(1 for item in source.rglob("*") if item.is_file())


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
            if child.is_dir():
                copied += copy_tree_merge(child, target)
            elif child.is_file():
                if target.exists():
                    continue
                shutil.copy2(child, target)
                copied += 1
        return copied

    if name == "depth_maps":
        src = source / "visuals" / "depth_maps"
        dst = output / "visuals" / "depth_maps"
        return copy_tree_merge(src, dst)

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
        if not src.exists() or src.is_symlink():
            continue
        if src.is_file():
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        elif src.is_dir():
            copied += copy_tree_merge(src, dst)
    return copied




def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _negative_prompt_sha256(job: dict) -> str:
    values = [
        "wrong story event", "unrelated characters", "unrelated props",
        "wrong location", "wrong time period", "distorted face",
        "unrealistic anatomy", "extra fingers", "missing fingers",
        "fused limbs", "duplicated people", "plastic skin", "waxy face",
        "floating objects", "captions", "subtitles", "unintended text",
        "logo", "watermark", "low resolution", "blurry main subject",
        "cartoon", "anime", "comic-book illustration", "2D drawing",
        "3D animated character", "body animation", "face animation",
        "character animation", "motion blur",
    ]
    extra = job.get("negative_prompt")
    if extra:
        values.append(str(extra))
    text = ", ".join(dict.fromkeys(values))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _image_reuse_signature(row: dict, negative_hash: str) -> str:
    scene_number = row.get("global_scene", row.get("global_scene_number", row.get("scene")))
    try:
        part = int(row.get("part"))
        global_scene = int(scene_number)
    except (TypeError, ValueError):
        return ""
    identity = {
        "scene_id": str(row.get("scene_id") or "").strip(),
        "part": part,
        "global_scene": global_scene,
        "role": str(row.get("role") or "").strip(),
        "prompt_sha256": str(row.get("prompt_sha256") or "").strip(),
        "negative_prompt_sha256": negative_hash,
        "size": str(row.get("image_size") or "").strip(),
        "config_signature": str(row.get("config_signature") or "").strip(),
        "image_path": str(row.get("image_path") or row.get("image") or row.get("file") or "").strip(),
    }
    raw = json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def restore_partial_visual_images(source: Path, output: Path) -> int:
    """Restore only individually verified images from an incomplete visual stage."""
    visual_root = source / "visuals"
    jobs_path = visual_root / "visual_jobs.json"
    jobs_manifest = read_json(visual_root / "visual_jobs_manifest.json")
    image_path = visual_root / "image_manifest.json"
    generation_path = visual_root / "image_generation_manifest.json"
    image_doc = read_json(image_path)
    generation_doc = read_json(generation_path)

    if not all((jobs_path.is_file(), jobs_manifest, image_doc, generation_doc)):
        return 0
    if jobs_manifest.get("status") not in SUCCESS_STATES:
        return 0

    source_fingerprint = str(jobs_manifest.get("source_fingerprint") or "").strip()
    config_signature = str(jobs_manifest.get("config_signature") or "").strip()
    format_name = str(jobs_manifest.get("format") or "").strip().lower()
    expected_size = "720x1280" if format_name == "short" else (
        "1280x720" if format_name == "full" else ""
    )
    if (
        not re.fullmatch(r"[0-9a-fA-F]{64}", source_fingerprint)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", config_signature)
        or not expected_size
    ):
        return 0

    for document in (image_doc, generation_doc):
        if (
            document.get("source_fingerprint") != source_fingerprint
            or document.get("config_signature") != config_signature
            or str(document.get("format") or "").lower() != format_name
            or str(document.get("image_size") or "") != expected_size
            or not isinstance(document.get("scenes"), list)
        ):
            return 0

    try:
        jobs_document = json.loads(jobs_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    jobs = (
        jobs_document if isinstance(jobs_document, list)
        else jobs_document.get("jobs", []) if isinstance(jobs_document, dict)
        else []
    )
    if not isinstance(jobs, list) or not jobs:
        return 0
    jobs_by_id = {
        str(row.get("scene_id") or "").strip(): row
        for row in jobs if isinstance(row, dict) and row.get("scene_id")
    }
    image_rows = {
        str(row.get("scene_id") or "").strip(): row
        for row in image_doc["scenes"] if isinstance(row, dict) and row.get("scene_id")
    }
    generation_rows = {
        str(row.get("scene_id") or "").strip(): row
        for row in generation_doc["scenes"] if isinstance(row, dict) and row.get("scene_id")
    }
    if (
        not image_rows or set(image_rows) != set(generation_rows)
        or set(image_rows) != set(jobs_by_id)
    ):
        return 0

    destination_visuals = output / "visuals"
    destination_image_manifest = destination_visuals / "image_manifest.json"
    destination_generation_manifest = destination_visuals / "image_generation_manifest.json"
    destination_visuals.mkdir(parents=True, exist_ok=True)

    # Merge same-input candidates across completed runs, including failed and
    # cancelled conclusions. Never replace a verified row with a failed row.
    for manifest_path, incoming in (
        (destination_image_manifest, image_doc),
        (destination_generation_manifest, generation_doc),
    ):
        existing = read_json(manifest_path) if manifest_path.is_file() else None
        if existing is None:
            continue
        if (
            existing.get("source_fingerprint") != source_fingerprint
            or existing.get("config_signature") != config_signature
            or str(existing.get("format") or "").lower() != format_name
        ):
            return 0
        existing_rows = {
            str(row.get("scene_id") or "").strip(): row
            for row in existing.get("scenes", [])
            if isinstance(row, dict) and row.get("scene_id")
        }
        for row in incoming.get("scenes", []):
            if not isinstance(row, dict) or not row.get("scene_id"):
                continue
            key = str(row["scene_id"]).strip()
            previous = existing_rows.get(key)
            if previous and str(previous.get("status", "")).lower() in {"generated", "existing", "completed"}:
                if str(row.get("status", "")).lower() not in {"generated", "existing", "completed"}:
                    continue
            existing_rows[key] = row
        existing.update({key: value for key, value in incoming.items() if key != "scenes"})
        existing["scenes"] = list(existing_rows.values())
        if manifest_path == destination_image_manifest:
            image_doc = existing
        else:
            generation_doc = existing

    ready_statuses = {"generated", "existing", "completed"}
    verified = 0
    progress_rows = []
    for scene_id, image_row in image_rows.items():
        generation_row = generation_rows[scene_id]
        if (
            str(image_row.get("status") or "").lower() not in ready_statuses
            or str(generation_row.get("status") or "").lower() not in ready_statuses
        ):
            continue
        identity_fields = (
            "image_path", "image_sha256", "prompt_sha256",
            "prompt_signature", "job_signature", "config_signature",
            "source_fingerprint",
        )
        if any(image_row.get(key) != generation_row.get(key) for key in identity_fields):
            continue
        if (
            image_row.get("source_fingerprint") != source_fingerprint
            or image_row.get("config_signature") != config_signature
            or str(image_row.get("image_size") or "") != expected_size
            or not re.fullmatch(r"[0-9a-fA-F]{64}", str(image_row.get("image_sha256") or ""))
            or not re.fullmatch(r"[0-9a-fA-F]{64}", str(image_row.get("prompt_sha256") or ""))
        ):
            continue

        relative = str(
            image_row.get("image_path") or image_row.get("image") or image_row.get("file") or ""
        ).replace("\\", "/")
        parts = PurePosixPath(relative).parts
        if (
            len(parts) < 4 or parts[:3] != ("output", "visuals", "images")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            continue
        source_file = source.joinpath(*parts[1:])
        destination_file = output.joinpath(*parts[1:])
        if source_file.is_symlink() or not source_file.is_file():
            continue
        try:
            with source_file.open("rb") as stream:
                header = stream.read(24)
            if (
                len(header) < 24
                or header[:8] != b"\x89PNG\r\n\x1a\n"
                or _sha256_file(source_file).lower() != str(image_row["image_sha256"]).lower()
            ):
                continue
            width, height = struct.unpack(">II", header[16:24])
            if (
                f"{width}x{height}" != expected_size
                or int(image_row.get("width", 0)) != width
                or int(image_row.get("height", 0)) != height
            ):
                continue
        except (OSError, TypeError, ValueError, struct.error):
            continue

        job = jobs_by_id[scene_id]
        negative_hash = _negative_prompt_sha256(job)
        reuse_signature = _image_reuse_signature(image_row, negative_hash)
        if not reuse_signature:
            continue
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        if destination_file.exists():
            if destination_file.is_symlink() or _sha256_file(destination_file).lower() != str(image_row["image_sha256"]).lower():
                continue
        else:
            shutil.copy2(source_file, destination_file)

        for row in (image_row, generation_row):
            row["negative_prompt_sha256"] = negative_hash
            row["reuse_signature"] = reuse_signature
        progress_rows.append({
            "scene_id": scene_id,
            "part": int(job.get("part", image_row.get("part", 0))),
            "global_scene": int(job.get("global_scene", image_row.get("global_scene", 0))),
            "role": str(job.get("role") or image_row.get("role") or "main"),
            "image_path": str(image_row["image_path"]),
            "prompt_signature": str(image_row.get("prompt_signature") or ""),
            "source_scene_signature": str(image_row.get("source_scene_signature") or job.get("source_scene_signature") or ""),
            "source_fingerprint": source_fingerprint,
            "config_signature": config_signature,
            "status": "existing",
            "image_sha256": str(image_row["image_sha256"]).lower(),
            "image_integrity": "sha256_png_dimensions_verified",
            "semantic_verification": image_row.get("semantic_verification", "pending_external_vision_verification"),
            "negative_prompt_sha256": negative_hash,
            "reuse_signature": reuse_signature,
        })
        verified += 1

    if not verified:
        return 0

    for document, path in (
        (image_doc, destination_image_manifest),
        (generation_doc, destination_generation_manifest),
    ):
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # Rebuild the progress file consumed by generate_visuals.py. A verified
    # image on disk is not reusable until its scene/prompt/config record exists.
    progress_path = output / "checkpoints" / "visual_jobs_progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    previous_progress = read_json(progress_path) if progress_path.is_file() else {}
    previous_rows = previous_progress.get("scene_progress", []) if isinstance(previous_progress, dict) else []
    merged = {
        str(row.get("scene_id")) + "|" + str(row.get("prompt_signature")): row
        for row in previous_rows
        if isinstance(row, dict)
        and row.get("scene_id") and row.get("prompt_signature")
        and row.get("source_fingerprint") == source_fingerprint
        and row.get("config_signature") == config_signature
    }
    for row in progress_rows:
        if row["prompt_signature"] and row["source_scene_signature"]:
            merged[row["scene_id"] + "|" + row["prompt_signature"]] = row
    rows = list(merged.values())
    completed_ids = {row["scene_id"] for row in rows}
    total = len(jobs_by_id)
    progress = {
        "version": "checkpoint-manager-restored-visual-progress-v1",
        "status": "ready",
        "stage": "visual_jobs_prepared",
        "source_fingerprint": source_fingerprint,
        "config_signature": config_signature,
        "total_jobs": total,
        "prepared_jobs": total,
        "completed_jobs": len(completed_ids),
        "remaining_jobs": max(0, total - len(completed_ids)),
        "scene_progress": rows,
        "restored_from_checkpoint": True,
    }
    progress_path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"Restored {verified} individually verified partial scene images and rebuilt visual progress records.")
    return verified


def restore_partial_tts_chunks(source: Path, output: Path) -> int:
    """Restore individually verified TTS chunk files from an interrupted run."""
    checkpoint = source / "checkpoints" / "tts_progress.json"
    data = read_json(checkpoint)
    if not isinstance(data, dict):
        return 0

    # generate_tts.py writes these exact SHA256 field names. They must be
    # recognized independently of a completed narration-stage manifest.
    for key in ("input_sha256", "config_sha256"):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(data.get(key) or "")):
            return 0

    records = data.get("completed_chunks")
    if not isinstance(records, list):
        return 0

    restored = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        job_id = str(record.get("job_id") or "").strip()
        digest = str(record.get("sha256") or "").strip().lower()
        if not job_id or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        try:
            duration = float(record.get("duration"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not (0.0 < duration < 3600.0):
            continue

        source_file = _checkpoint_output_file(source, record.get("output_path"))
        if source_file is None or _sha256_file(source_file).lower() != digest:
            continue

        try:
            relative = source_file.relative_to(source.resolve())
        except ValueError:
            continue
        destination_file = output / relative
        if destination_file.exists():
            if destination_file.is_symlink() or not destination_file.is_file():
                continue
            if _sha256_file(destination_file).lower() != digest:
                continue
            continue

        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)
        if _sha256_file(destination_file).lower() != digest:
            destination_file.unlink(missing_ok=True)
            continue
        restored += 1

    if restored:
        log(f"Restored {restored} individually SHA256-verified partial TTS chunks.")
    return restored


def _progress_identity_compatible(left: dict, right: dict) -> bool:
    identity_keys = (
        "input_fingerprint", "input_hash", "input_sha256",
        "source_fingerprint", "source_signature", "config_signature",
        "generation_signature", "cache_signature", "manifest_signature",
    )
    for key in identity_keys:
        a, b = left.get(key), right.get(key)
        if a not in (None, "") and b not in (None, "") and a != b:
            return False
    return True


def _progress_row_key(row: Any) -> str:
    if isinstance(row, (str, int)) and not isinstance(row, bool):
        return f"value:{row}"
    if not isinstance(row, dict):
        return ""
    for key in ("scene_id", "chunk_id", "job_id", "track_id", "artifact_id", "id", "key"):
        value = row.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    if row.get("part") is not None and row.get("scene") is not None:
        return f"part-scene:{row.get('part')}:{row.get('scene')}:{row.get('role', '')}"
    return _story_fingerprint(row)


def _merge_progress_document(existing: dict, incoming: dict) -> dict | None:
    if not _progress_identity_compatible(existing, incoming):
        return None
    merged = dict(existing)
    row_fields = {
        "scene_progress", "jobs", "chunks", "records", "items",
        "scene_records", "completed_jobs", "completed_chunks",
    }
    for key, value in incoming.items():
        if key in row_fields and isinstance(value, list):
            old_rows = merged.get(key, [])
            old_rows = old_rows if isinstance(old_rows, list) else []
            by_key = {_progress_row_key(row): row for row in old_rows if _progress_row_key(row)}
            for row in value:
                row_key = _progress_row_key(row)
                if not row_key:
                    continue
                previous = by_key.get(row_key)
                if previous is None:
                    by_key[row_key] = row
                    continue
                previous_ready = (
                    isinstance(previous, dict)
                    and str(previous.get("status", "")).lower() in SUCCESS_STATES
                )
                incoming_ready = (
                    isinstance(row, dict)
                    and str(row.get("status", "")).lower() in SUCCESS_STATES
                )
                previous_hash = (
                    isinstance(previous, dict)
                    and any(previous.get(field) for field in ("sha256", "image_sha256", "audio_sha256", "video_sha256"))
                )
                incoming_hash = (
                    isinstance(row, dict)
                    and any(row.get(field) for field in ("sha256", "image_sha256", "audio_sha256", "video_sha256"))
                )
                if (incoming_ready and incoming_hash) or not (previous_ready and previous_hash):
                    by_key[row_key] = row
            merged[key] = list(by_key.values())
        elif key in {"completed_tracks", "completed_jobs", "completed_chunks"} and isinstance(value, list):
            previous = merged.get(key, [])
            previous = previous if isinstance(previous, list) else []
            merged[key] = list(dict.fromkeys(previous + value))
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            for nested_key, nested_value in value.items():
                previous_value = nested.get(nested_key)
                if isinstance(previous_value, dict) and isinstance(nested_value, dict):
                    old_ready = str(previous_value.get("status", "")).lower() in SUCCESS_STATES
                    new_ready = str(nested_value.get("status", "")).lower() in SUCCESS_STATES
                    if new_ready and not old_ready:
                        nested[nested_key] = nested_value
                elif nested_key not in nested:
                    nested[nested_key] = nested_value
            merged[key] = nested
        elif key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    return merged


def _copy_verified_progress_artifacts(document: dict, source: Path, output: Path) -> int:
    """Restore individual files referenced by resumable records after SHA256 verification."""
    path_keys = (
        "image_path", "audio_path", "video_path", "depth_path",
        "caption_path", "font_path", "thumbnail_path", "output_path",
        "srt_path", "wav_path", "video", "file", "path", "output",
    )
    hash_keys = (
        "image_sha256", "audio_sha256", "video_sha256", "depth_sha256",
        "caption_sha256", "font_sha256", "thumbnail_sha256",
        "source_sha256", "manifest_sha256", "sha256", "file_sha256",
    )
    copied = 0
    stack = [document]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            raw_path = next((value.get(key) for key in path_keys if isinstance(value.get(key), str) and value.get(key).strip()), None)
            expected_hash = next((value.get(key) for key in hash_keys if isinstance(value.get(key), str) and re.fullmatch(r"[a-fA-F0-9]{64}", value.get(key).strip())), None)
            if raw_path and expected_hash:
                relative = Path(raw_path.replace("\\", "/"))
                if not relative.is_absolute() and ".." not in relative.parts:
                    parts = list(relative.parts)
                    if parts and parts[0].casefold() == "output":
                        parts = parts[1:]
                    if parts:
                        source_path = source.joinpath(*parts)
                        destination_path = output.joinpath(*parts)
                        try:
                            if (
                                source_path.is_file()
                                and not source_path.is_symlink()
                                and _sha256_file(source_path).casefold() == expected_hash.casefold()
                            ):
                                if destination_path.is_symlink():
                                    continue
                                if destination_path.is_file():
                                    if _sha256_file(destination_path).casefold() == expected_hash.casefold():
                                        pass
                                    else:
                                        continue
                                else:
                                    destination_path.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(source_path, destination_path)
                                    if _sha256_file(destination_path).casefold() != expected_hash.casefold():
                                        destination_path.unlink(missing_ok=True)
                                        continue
                                    copied += 1
                        except (OSError, RuntimeError, ValueError):
                            continue
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return copied


def merge_progress_checkpoints(source: Path, output: Path) -> int:
    roots = (
        "checkpoints",
        "story",
        "story/checkpoints",
        "narration",
        "narration/checkpoints",
        "visuals",
        "visuals/checkpoints",
        "visuals/depth_maps",
        "photo_motion",
        "photo_motion/checkpoints",
        "sound_design",
        "sound_design/checkpoints",
        "sfx",
        "sfx/checkpoints",
        "render",
        "render/checkpoints",
        "scene_renders",
        "parts",
        "captions",
        "thumbnails",
        "metadata",
        "music",
        "audio",
        "fonts",
        "scenes",
    )
    changed = 0
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
                    "config_signature", "config_hash", "config_sha256",
                    "source_fingerprint", "source_signature", "source_hash",
                    "input_fingerprint", "input_hash", "input_sha256",
                    "input_signature", "generation_signature", "cache_signature",
                    "manifest_signature", "job_signature", "signature", "fingerprint",
                )
            )
            if not has_stage_identity:
                continue
            changed += _copy_verified_progress_artifacts(data, source, output)
            relative = candidate.relative_to(source)
            destination = output / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, destination)
                changed += 1
                continue
            if destination.is_symlink() or not destination.is_file():
                continue
            existing = read_json(destination)
            if not isinstance(existing, dict):
                # A prior buggy checkpoint writer may have left concatenated
                # JSON documents. The incoming source is already parsed and
                # has a stage identity; replace the corrupt destination with
                # this valid checkpoint instead of silently retaining damage.
                destination.write_text(
                    json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                changed += 1
                continue
            merged = _merge_progress_document(existing, data)
            if merged is None:
                continue
            encoded = json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            if destination.read_text(encoding="utf-8-sig") != encoded:
                destination.write_text(encoded, encoding="utf-8")
                changed += 1
    return changed


def quarantine_invalid_json_files(output: Path) -> int:
    """Keep corrupt restored JSON for diagnosis but prevent unsafe reuse."""
    quarantined = 0
    if not output.is_dir():
        return 0
    for path in sorted(output.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        digest = _sha256_file(path)[:12]
        target = path.with_name(path.name + f".invalid-{digest}")
        if target.exists():
            continue
        try:
            path.replace(target)
            quarantined += 1
            log("Quarantined invalid checkpoint JSON:", path, "->", target.name)
        except OSError as exc:
            log("Could not quarantine invalid checkpoint JSON:", path, exc)
    return quarantined


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
    # gh api does not allow --slurp together with --jq. Apply jq to each
    # paginated response and parse one run ID per output line instead.
    # status=completed includes success, failure and cancelled conclusions.
    query = ["status=completed", "per_page=100"]
    if branch and branch.casefold() not in {"all", "*"}:
        query.insert(0, f"branch={branch}")
    endpoint = (
        f"repos/{repository}/actions/workflows/{workflow}/runs?"
        + "&".join(query)
    )
    command = [
        "gh", "api", "--paginate", "--jq",
        '.workflow_runs[] | select(.status == "completed") | .id',
        endpoint,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        log("Checkpoint search unavailable; continuing without cross-run restore:", result.stderr.strip())
        return []

    run_ids = []
    malformed = []
    stdout = (result.stdout or "").strip()

    # --jq normally emits one numeric ID per line. Some gh versions,
    # wrappers, and test doubles return the original JSON response instead.
    # Accept both formats while retaining completed runs of every conclusion.
    parsed_json = None
    if stdout.startswith(("[", "{")):
        try:
            parsed_json = json.loads(stdout)
        except (TypeError, ValueError):
            parsed_json = None

    if isinstance(parsed_json, dict):
        records = parsed_json.get("workflow_runs", [])
    elif isinstance(parsed_json, list):
        records = parsed_json
    else:
        records = None

    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            if str(record.get("status") or "").lower() != "completed":
                continue
            raw_id = record.get("id", record.get("databaseId"))
            try:
                run_id = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                malformed.append(str(raw_id)[:120])
                continue
            if run_id > 0 and run_id not in run_ids:
                run_ids.append(run_id)
    else:
        for line in stdout.splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                run_id = int(value)
            except ValueError:
                malformed.append(value[:120])
                continue
            if run_id > 0 and run_id not in run_ids:
                run_ids.append(run_id)

    if malformed:
        log(
            "Checkpoint run list contained malformed entries; ignored:",
            "; ".join(malformed[:5]),
        )
    return run_ids[:limit] if limit else run_ids


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
    restored_partial_images = 0

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

            restored_partial_images += restore_partial_visual_images(saved, output)
            restored_tts_chunks = restore_partial_tts_chunks(saved, output)
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

    quarantined_json = quarantine_invalid_json_files(output)

    report = {
        "status": "completed",
        "runs_examined": examined,
        "run_limit": args.limit if args.limit else "all",
        "restored_stages": restored,
        "restored_stage_count": len(restored),
        "progress_checkpoint_files_merged": progress_checkpoints,
        "invalid_json_quarantined": quarantined_json,
        "restored_partial_visual_images": restored_partial_images,
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
    command.add_argument("--limit", type=int, default=0, help="Maximum runs to inspect; 0 inspects every paginated run.")
    command.add_argument("--input-hash", required=True)
    command.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero (all runs) or a positive integer")
    return args




# Record checkpoint restore activity without changing restore semantics.
_photo_motion_original_restore = restore
def restore(arguments):
    import time as _pm_time
    _pm_started = _pm_time.monotonic()
    try:
        try:
            from production_events import record_event as _pm_record
        except ImportError:
            from scripts.production_events import record_event as _pm_record
        _pm_record("checkpoint_restore", "running")
    except Exception:
        _pm_record = None
    try:
        _pm_result = _photo_motion_original_restore(arguments)
        if _pm_record: _pm_record("checkpoint_restore", "completed", started_at=_pm_started)
        return _pm_result
    except BaseException as _pm_exc:
        if _pm_record: _pm_record("checkpoint_restore", "failed", error=f"{type(_pm_exc).__name__}: {_pm_exc}", started_at=_pm_started)
        raise

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
