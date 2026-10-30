
#!/usr/bin/env python3
"""Final output validator for Katha Lok AI photo-motion pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

OUT = ROOT / "output"

PHOTO = OUT / "photo_motion" / "photo_motion_manifest.json"
AUDIO = OUT / "narration" / "audio_jobs.json"
CAPTIONS = OUT / "captions" / "captions.json"
SOUND = OUT / "sound_design" / "sound_design_manifest.json"
SFX = OUT / "sound_design" / "sfx_cues.json"
RENDER = OUT / "scene_renders" / "render_manifest.json"
THUMBNAIL = OUT / "thumbnail.jpg"
THUMBNAIL_MANIFEST = OUT / "thumbnail" / "thumbnail_manifest.json"
METADATA = OUT / "youtube_metadata.txt"
SRT = OUT / "captions" / "final_captions.srt"
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v29-package-compatible"
MIN_VIDEO_BYTES = 1000
MIN_AUDIO_BYTES = 100


def log(*args):
    print(*args, flush=True)


def fail(message):
    raise RuntimeError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, optional=False):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional and not path.exists():
            return None
        fail(f"Cannot read {path.relative_to(ROOT)}: {exc}")

    return value


def write_report(data):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".json.tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(REPORT)
    finally:
        temporary.unlink(missing_ok=True)


def rows(data, *keys):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def pick(row, *keys):
    if not isinstance(row, dict):
        return None

    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def safe_file(value, label, minimum=1):
    if not isinstance(value, str) or not value.strip():
        fail(f"{label}: file path missing")

    path = Path(value.strip())

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        fail(f"{label}: path escapes repository")

    if not path.is_file():
        fail(f"{label}: file not found: {path}")

    if path.stat().st_size < minimum:
        fail(f"{label}: file empty or too small: {path.name}")

    return path


def media_info(path):
    executable = shutil.which("ffprobe")

    if not executable:
        fail("ffprobe is not installed")

    result = subprocess.run(
        [
            executable,
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode != 0:
        fail(
            f"ffprobe failed for {path.name}: "
            f"{result.stderr[-500:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        fail(f"Invalid media information: {path.name}")

    if not math.isfinite(duration) or duration <= 0:
        fail(f"Invalid media duration: {path.name}")

    streams = data.get("streams", [])

    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )

    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )

    return {
        "duration": duration,
        "video": video is not None,
        "audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def validate_video(path, expected_size, label):
    info = media_info(path)

    if not info["video"] or not info["audio"]:
        fail(f"{label}: audio/video stream missing")

    actual_size = (info["width"], info["height"])

    if actual_size != expected_size:
        fail(
            f"{label}: expected {expected_size[0]}x{expected_size[1]}, "
            f"got {actual_size[0]}x{actual_size[1]}"
        )

    return info


def expected_scene_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def validate_scene_rows(items, parts, scenes, label):
    expected = expected_scene_keys(parts, scenes)

    if not isinstance(items, list):
        fail(f"{label}: scene list missing")

    if len(items) != len(expected):
        fail(
            f"{label}: expected {len(expected)} scenes, "
            f"found {len(items)}"
        )

    mapping = {}

    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            fail(f"{label}: invalid scene row {index}")

        try:
            part = int(row["part"])
            scene = int(
                row.get("local_scene", row.get("scene", 0))
            )
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError):
            fail(f"{label}: invalid scene mapping at row {index}")

        wanted_part, wanted_scene = expected[index - 1]

        if (part, scene) != (wanted_part, wanted_scene):
            fail(
                f"{label}: wrong part/scene order at row {index}: "
                f"{part}/{scene}"
            )

        if global_scene != index:
            fail(
                f"{label}: global_scene mismatch at row {index}: "
                f"{global_scene}"
            )

        key = (part, scene)

        if key in mapping:
            fail(f"{label}: duplicate scene mapping {key}")

        mapping[key] = row

    return mapping


def validate_photo(mode, parts, scenes):
    data = read_json(PHOTO)

    if not isinstance(data, dict):
        fail("Photo-motion manifest must be a JSON object")

    if data.get("status") != "completed":
        fail("Photo-motion manifest is not completed")

    if str(data.get("format", "")).lower() != mode:
        fail("Photo-motion FORMAT mismatch")

    expected_count = parts * scenes

    for field in ("expected_scene_count", "completed_count"):
        try:
            actual = int(data.get(field, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected_count:
            fail(
                f"Photo-motion {field} mismatch: "
                f"expected {expected_count}, got {actual}"
            )

    mapping = validate_scene_rows(
        rows(data, "scenes", "completed_scenes"),
        parts,
        scenes,
        "Photo motion",
    )

    checked = 0

    for key, row in mapping.items():
        if row.get("status") != "completed":
            fail(f"Photo-motion scene incomplete: {key}")

        path = safe_file(
            pick(row, "video", "source_video", "output", "path"),
            f"Photo-motion scene {key}",
            MIN_VIDEO_BYTES,
        )

        info = media_info(path)

        if not info["video"]:
            fail(f"Photo-motion scene has no video stream: {key}")

        declared_hash = row.get("video_sha256") or row.get("sha256")

        if declared_hash and declared_hash != sha256(path):
            fail(f"Photo-motion hash mismatch: {key}")

        checked += 1

    return {
        "status": "valid",
        "scene_count": checked,
    }


def validate_audio(parts, scenes):
    data = read_json(AUDIO)

    if not isinstance(data, dict):
        fail("Narration audio manifest must be a JSON object")

    if data.get("status") != "completed":
        fail("Narration audio manifest is not completed")

    jobs = rows(data, "jobs", "audio_jobs", "scenes")

    if not jobs:
        fail("Narration audio jobs are missing")

    expected = set(expected_scene_keys(parts, scenes))
    found = set()
    checked = 0

    for index, row in enumerate(jobs, 1):
        if not isinstance(row, dict):
            fail(f"Invalid narration row {index}")

        try:
            key = (
                int(row["part"]),
                int(row.get("scene", row.get("local_scene", 0))),
            )
        except (KeyError, TypeError, ValueError):
            fail(f"Invalid narration mapping row {index}")

        if key in found:
            fail(f"Duplicate narration scene: {key}")

        if key not in expected:
            fail(f"Unexpected narration scene: {key}")

        if row.get("status") != "completed":
            fail(f"Narration scene incomplete: {key}")

        path = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}",
            MIN_AUDIO_BYTES,
        )

        info = media_info(path)

        if not info["audio"]:
            fail(f"Narration has no audio stream: {key}")

        declared_hash = row.get("sha256")

        if declared_hash and declared_hash != sha256(path):
            fail(f"Narration hash mismatch: {key}")

        found.add(key)
        checked += 1

    if found != expected:
        fail(
            "Narration scene mapping mismatch; "
            f"missing={sorted(expected - found)}"
        )

    return {
        "status": "valid",
        "scene_count": checked,
    }


def validate_captions(mode, parts, scenes):
    data = read_json(CAPTIONS)

    if not isinstance(data, dict):
        fail("Caption manifest must be a JSON object")

    if data.get("status") != "completed":
        fail("Caption manifest is not completed")

    if str(data.get("format", "")).lower() != mode:
        fail("Caption FORMAT mismatch")

    caption_rows = rows(data, "captions")

    if not caption_rows:
        fail("Caption rows are missing")

    expected = set(expected_scene_keys(parts, scenes))
    found = set()

    for index, row in enumerate(caption_rows, 1):
        if not isinstance(row, dict):
            fail(f"Invalid caption row {index}")

        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            fail(f"Invalid caption mapping row {index}")

        if key in found:
            fail(f"Duplicate caption scene: {key}")

        text = row.get("text")

        if not isinstance(text, str) or not text.strip():
            fail(f"Caption text missing: {key}")

        found.add(key)

    if found != expected:
        fail(
            "Caption scene mapping mismatch; "
            f"missing={sorted(expected - found)}"
        )

    srt = safe_file(SRT.relative_to(ROOT).as_posix(), "Final SRT", 10)
    content = srt.read_text(encoding="utf-8-sig")

    if "-->" not in content:
        fail("Final SRT has no timing cues")

    return {
        "status": "valid",
        "scene_count": len(found),
        "srt": srt.relative_to(ROOT).as_posix(),
        "srt_sha256": sha256(srt),
        "timing_note": (
            "SRT timing is checked for presence; "
            "word-level alignment is not independently verified."
        ),
    }


def validate_sound():
    data = read_json(SOUND)

    if not isinstance(data, dict):
        fail("Sound-design manifest must be a JSON object")

    if data.get("status") != "completed":
        fail("Sound-design manifest is not completed")

    tracks = data.get("tracks")

    if not isinstance(tracks, dict):
        fail("Sound-design tracks object missing")

    checked_tracks = {}

    for name, record in tracks.items():
        if record is None:
            checked_tracks[name] = "disabled"
            continue

        if not isinstance(record, dict):
            fail(f"Invalid sound track record: {name}")

        if record.get("status") != "completed":
            fail(f"Sound track incomplete: {name}")

        path = safe_file(
            pick(record, "path", "file", "output", "audio_path"),
            f"Sound track {name}",
            MIN_AUDIO_BYTES,
        )

        if not media_info(path)["audio"]:
            fail(f"Sound track has no audio stream: {name}")

        declared_hash = record.get("sha256")

        if declared_hash and declared_hash != sha256(path):
            fail(f"Sound track hash mismatch: {name}")

        checked_tracks[name] = path.relative_to(ROOT).as_posix()

    cue_data = read_json(SFX, optional=True)
    cue_count = 0

    if cue_data is not None:
        if not isinstance(cue_data, dict):
            fail("SFX cue manifest must be a JSON object")

        if cue_data.get("status") != "completed":
            fail("SFX cue manifest is not completed")

        for index, cue in enumerate(
            rows(cue_data, "cues", "items"),
            1,
        ):
            if not isinstance(cue, dict):
                fail(f"Invalid SFX cue row {index}")

            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
                timestamp = float(cue.get("time", 0))
                gain = float(cue.get("gain", 0))
            except (KeyError, TypeError, ValueError):
                fail(f"Invalid SFX cue timing row {index}")

            if part < 1 or scene < 1:
                fail(f"Invalid SFX scene mapping: {index}")

            if not math.isfinite(timestamp) or timestamp < 0:
                fail(f"Invalid SFX timestamp: {index}")

            if not math.isfinite(gain) or gain < 0:
                fail(f"Invalid SFX gain: {index}")

            path = safe_file(
                pick(cue, "file", "path", "audio", "output"),
                f"SFX cue {index}",
                MIN_AUDIO_BYTES,
            )

            if not media_info(path)["audio"]:
                fail(f"SFX cue has no audio stream: {index}")

            cue_count += 1

    return {
        "status": "valid",
        "tracks": checked_tracks,
        "sfx_cue_count": cue_count,
    }


def validate_render(mode, parts, scenes, expected_size):
    data = read_json(RENDER)

    if not isinstance(data, dict):
        fail("Render manifest must be a JSON object")

    if data.get("status") != "completed":
        fail("Render manifest is not completed")

    if str(data.get("format", "")).lower() != mode:
        fail("Render manifest FORMAT mismatch")

    if data.get("captions_burned") is not True:
        fail("Render manifest does not confirm burned captions")

    expected_count = parts * scenes

    try:
        completed_count = int(data.get("completed_count", -1))
    except (TypeError, ValueError):
        completed_count = -1

    if completed_count != expected_count:
        fail(
            f"Render completed_count mismatch: "
            f"{completed_count}/{expected_count}"
        )

    scene_rows = rows(data, "completed_scenes", "scenes")

    mapping = validate_scene_rows(
        scene_rows,
        parts,
        scenes,
        "Final render",
    )

    scene_total = 0.0
    scene_records = []

    for key, row in mapping.items():
        if row.get("status") != "completed":
            fail(f"Rendered scene incomplete: {key}")

        if row.get("captions_burned") is not True:
            fail(f"Captions not confirmed burned into scene: {key}")

        path = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Rendered scene {key}",
            MIN_VIDEO_BYTES,
        )

        info = validate_video(path, expected_size, f"Rendered scene {key}")
        scene_total += info["duration"]

        declared_hash = row.get("sha256")

        if declared_hash and declared_hash != sha256(path):
            fail(f"Rendered scene hash mismatch: {key}")

        scene_records.append({
            "part": key[0],
            "scene": key[1],
            "duration": round(info["duration"], 3),
            "sha256": sha256(path),
        })

    part_rows = data.get("parts")

    if not isinstance(part_rows, list) or len(part_rows) != parts:
        fail(f"Expected exactly {parts} part videos")

    part_total = 0.0
    part_records = []

    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            try:
                part_number = int(row.get("part", index))
            except (TypeError, ValueError):
                fail(f"Invalid part number: {index}")

            if part_number != index:
                fail(f"Part order mismatch: {index}")

            if row.get("status") not in (None, "completed"):
                fail(f"Part video incomplete: {index}")

            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", MIN_VIDEO_BYTES)
        info = validate_video(path, expected_size, f"Part video {index}")

        part_total += info["duration"]

        part_records.append({
            "part": index,
            "duration": round(info["duration"], 3),
            "sha256": sha256(path),
        })

    final = safe_file(data.get("final_video"), "Final video", MIN_VIDEO_BYTES)
    alias = safe_file(data.get("alias"), "Format alias", MIN_VIDEO_BYTES)

    final_info = validate_video(final, expected_size, "Final video")
    alias_info = validate_video(alias, expected_size, "Format alias")

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )

    if alias.name != expected_alias:
        fail(f"Wrong selected-format alias: {alias.name}")

    if final.resolve() == alias.resolve():
        fail("Final video and format alias must be separate files")

    if abs(final_info["duration"] - alias_info["duration"]) > 1:
        fail("Final video and alias durations differ")

    if sha256(final) != sha256(alias):
        fail("Final video and alias content hashes differ")

    if abs(final_info["duration"] - part_total) > max(2, parts):
        fail("Final duration does not match part video durations")

    if abs(final_info["duration"] - scene_total) > max(2, expected_count):
        fail("Final duration does not match rendered scene durations")

    opposite = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )

    opposite_paths = (
        OUT / "videos" / opposite,
        OUT / opposite,
    )

    for path in opposite_paths:
        if path.exists():
            fail(f"Opposite-format output exists: {path.relative_to(ROOT)}")

    return {
        "status": "valid",
        "scene_count": expected_count,
        "part_count": parts,
        "final_video": final.relative_to(ROOT).as_posix(),
        "final_video_sha256": sha256(final),
        "alias": alias.relative_to(ROOT).as_posix(),
        "duration": round(final_info["duration"], 3),
        "resolution": list(expected_size),
        "scene_records": scene_records,
        "part_records": part_records,
    }


def validate_thumbnail(mode):
    path = safe_file(
        THUMBNAIL.relative_to(ROOT).as_posix(),
        "Thumbnail",
        MIN_VIDEO_BYTES,
    )

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            dimensions = image.size
            image_format = image.format

    except Exception as exc:
        fail(f"Invalid thumbnail: {exc}")

    if dimensions != (1280, 720) or image_format != "JPEG":
        fail(
            f"Thumbnail must be 1280x720 JPEG; "
            f"got {image_format} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST, optional=True)

    if manifest is not None:
        if not isinstance(manifest, dict):
            fail("Thumbnail manifest must be a JSON object")

        if manifest.get("status") != "completed":
            fail("Thumbnail manifest is not completed")

        if str(manifest.get("format", "")).lower() != mode:
            fail("Thumbnail manifest FORMAT mismatch")

        declared = pick(manifest, "thumbnail", "path", "output")

        if declared:
            declared_path = safe_file(declared, "Thumbnail manifest path")

            if declared_path.resolve() != path.resolve():
                fail("Thumbnail manifest points to a different file")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "resolution": list(dimensions),
    }


def validate_metadata():
    path = safe_file(
        METADATA.relative_to(ROOT).as_posix(),
        "YouTube metadata",
        10,
    )

    content = path.read_text(encoding="utf-8-sig").strip()

    if not content:
        fail("YouTube metadata is empty")

    sections = {}
    current = None

    for line in content.splitlines():
        normalized = (
            line.strip()
            .strip("[]")
            .rstrip(":")
            .strip()
            .upper()
        )

        if normalized in {"TITLE", "DESCRIPTION", "TAGS", "LANGUAGE"}:
            current = normalized
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.strip())

    title = " ".join(sections.get("TITLE", [])).strip()
    description = " ".join(sections.get("DESCRIPTION", [])).strip()
    tags = " ".join(sections.get("TAGS", [])).strip()

    if not title:
        fail("Metadata TITLE is empty")

    if not description:
        fail("Metadata DESCRIPTION is empty")

    if not tags:
        fail("Metadata TAGS is empty")

    if len(title) > 100:
        fail(f"Metadata title exceeds 100 characters: {len(title)}")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "title_length": len(title),
        "description_length": len(description),
        "tags_length": len(tags),
    }


def main():
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            fail(f"{command} is required")

    if not OUT.is_dir():
        fail("output/ directory missing")

    config = load_and_validate(ROOT / "Input" / "topic.txt")

    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in ("short", "full"):
        fail(f"Unsupported FORMAT: {mode}")

    if parts < 1 or scenes < 1:
        fail("PARTS and SCENES must be positive integers")

    expected_size = (
        (720, 1280)
        if mode == "short"
        else (1280, 720)
    )

    log("VALIDATION VERSION:", VERSION)
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES PER PART:", scenes)
    log("EXPECTED SCENES:", parts * scenes)

    checks = {}

    checks["photo_motion"] = validate_photo(
        mode, parts, scenes
    )

    checks["narration"] = validate_audio(
        parts, scenes
    )

    checks["captions"] = validate_captions(
        mode, parts, scenes
    )

    checks["sound_design"] = validate_sound()

    checks["render"] = validate_render(
        mode, parts, scenes, expected_size
    )

    checks["thumbnail"] = validate_thumbnail(mode)

    checks["metadata"] = validate_metadata()

    for name in (
        "photo_motion",
        "narration",
        "captions",
        "sound_design",
        "render",
        "thumbnail",
        "metadata",
    ):
        if checks[name].get("status") != "valid":
            fail(f"Validation check did not pass: {name}")

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "expected_scenes": parts * scenes,
        "resolution": list(expected_size),
        "checks": checks,
        "limitations": [
            "Caption timing is checked for SRT cue presence; "
            "word-level alignment is not independently verified.",
            "Manifest validation does not judge subjective story quality.",
            "Thumbnail validation checks file integrity and dimensions, "
            "not artistic quality.",
        ],
    }

    write_report(report)

    log("VALIDATION: PASSED")
    log("PACKAGE-COMPATIBLE CHECK STATUSES: valid")
    log("REPORT:", REPORT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FINAL VALIDATION FAILED:", str(exc))
        sys.exit(1)
