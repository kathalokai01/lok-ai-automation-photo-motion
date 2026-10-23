#!/usr/bin/env python3
"""Cross-manifest validation for Katha Lok AI photo-motion outputs."""

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
REPORT = OUT / "validation_report.json"

VERSION = "final-validator-v24-compatible-cross-check"
RESOLUTION = (1280, 720)


def log(*items):
    print(*items, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_report(data):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temp = REPORT.with_suffix(".json.tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(REPORT)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, optional=False):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"JSON पढ़ने में समस्या: {path}: {exc}") from exc
    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"JSON का root object/list नहीं है: {path}")
    return value


def rows(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
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
        raise RuntimeError(f"{label}: manifest में file path नहीं है")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"{label}: repository से बाहर का path")
    if not path.is_file():
        raise RuntimeError(f"{label}: file नहीं मिली: {path}")
    if path.stat().st_size < minimum:
        raise RuntimeError(f"{label}: file खाली या बहुत छोटी है: {path.name}")
    return path


def media_info(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe उपलब्ध नहीं है")

    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Media जाँच असफल: {path.name}: {result.stderr[-700:]}"
        )

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Media metadata गलत है: {path.name}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Media duration गलत है: {path.name}")

    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )

    return {
        "duration": duration,
        "video": video is not None,
        "audio": audio is not None,
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def require_video(path, label, require_audio=True):
    info = media_info(path)
    if not info["video"]:
        raise RuntimeError(f"{label}: video stream गायब है")
    if require_audio and not info["audio"]:
        raise RuntimeError(f"{label}: audio stream गायब है")
    actual = (info["width"], info["height"])
    if actual != RESOLUTION:
        raise RuntimeError(
            f"{label}: resolution {actual} है, "
            f"{RESOLUTION[0]}x{RESOLUTION[1]} अपेक्षित था"
        )
    return info


def expected_keys(parts, scenes):
    return [
        (part, scene)
        for part in range(1, parts + 1)
        for scene in range(1, scenes + 1)
    ]


def scene_map(data_rows, parts, scenes, label):
    expected = expected_keys(parts, scenes)
    if not isinstance(data_rows, list) or len(data_rows) != len(expected):
        count = len(data_rows) if isinstance(data_rows, list) else 0
        raise RuntimeError(
            f"{label}: {len(expected)} scenes चाहिए, {count} मिले"
        )

    result = {}
    for index, row in enumerate(data_rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"{label}: row {index} object नहीं है")
        try:
            part = int(row["part"])
            scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{label}: row {index} की mapping गलत है") from exc

        expected_part, expected_scene = expected[index - 1]
        if (part, scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(
                f"{label}: scene क्रम गलत है row {index}: "
                f"part={part}, scene={scene}, global={global_scene}"
            )

        key = (part, scene)
        if key in result:
            raise RuntimeError(f"{label}: duplicate scene {key}")
        result[key] = row
    return result


def validate_photo(parts, scenes, mode):
    data = read_json(PHOTO)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Photo-motion manifest completed नहीं है")
    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Photo-motion format और input format अलग हैं")

    mapping = scene_map(
        rows(data, "scenes", "videos", "items"),
        parts, scenes, "Photo motion",
    )
    verified = {}

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Photo-motion scene अधूरा है: {key}")
        path = safe_file(
            pick(row, "video", "video_path", "source_video",
                 "output", "path", "file"),
            f"Photo motion {key}", 1000,
        )
        info = require_video(path, f"Photo motion {key}", require_audio=False)
        verified[key] = {
            "path": path,
            "sha256": sha256(path),
            "duration": info["duration"],
        }

    if int(data.get("completed_count", -1)) != len(expected_keys(parts, scenes)):
        raise RuntimeError("Photo-motion completed_count गलत है")

    return verified


def validate_audio(parts, scenes):
    data = read_json(AUDIO)
    mapping = {}

    for row in rows(data, "jobs", "audio_jobs", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Narration manifest में गलत row है")
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Narration scene mapping गलत है") from exc

        if key in mapping:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        if str(row.get("status", "")).lower() != "completed":
            raise RuntimeError(f"Narration completed नहीं है: {key}")

        path = safe_file(
            pick(row, "output", "audio_path", "path", "file", "audio"),
            f"Narration {key}", 100,
        )
        info = media_info(path)
        if not info["audio"]:
            raise RuntimeError(f"Narration audio stream नहीं है: {key}")
        mapping[key] = {"path": path, "sha256": sha256(path)}

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            f"Narration mapping mismatch; missing={sorted(expected-set(mapping))}; "
            f"extra={sorted(set(mapping)-expected)}"
        )
    return mapping


def validate_captions(parts, scenes):
    data = read_json(CAPTIONS)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Captions manifest completed नहीं है")

    mapping = {}
    for row in rows(data, "captions", "scenes"):
        if not isinstance(row, dict):
            raise RuntimeError("Caption manifest row गलत है")
        try:
            key = (
                int(row["part"]),
                int(row.get("local_scene", row.get("scene", 0))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Caption scene mapping गलत है") from exc

        text = row.get("text", row.get("caption", row.get("caption_text", "")))
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Caption text खाली है: {key}")
        if key in mapping:
            raise RuntimeError(f"Duplicate caption: {key}")
        mapping[key] = text.strip()

    expected = set(expected_keys(parts, scenes))
    if set(mapping) != expected:
        raise RuntimeError(
            f"Caption mapping mismatch; missing={sorted(expected-set(mapping))}; "
            f"extra={sorted(set(mapping)-expected)}"
        )
    return mapping


def validate_sound():
    data = read_json(SOUND, optional=True)
    if data is None:
        return {"status": "warning", "reason": "Sound-design manifest missing"}

    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Sound-design manifest completed नहीं है")

    tracks = data.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks object गलत है")

    results = {}
    for name, row in tracks.items():
        if row is None:
            results[name] = "not_generated"
            continue
        if not isinstance(row, dict):
            raise RuntimeError(f"Sound track record गलत है: {name}")
        if row.get("status") != "completed":
            raise RuntimeError(f"Sound track completed नहीं है: {name}")

        value = pick(row, "path", "file", "output", "audio_path")
        if value:
            path = safe_file(value, f"Sound track {name}", 100)
            info = media_info(path)
            if not info["audio"]:
                raise RuntimeError(f"Sound track में audio नहीं: {name}")
            results[name] = {
                "status": "valid",
                "sha256": sha256(path),
                "path": path.relative_to(ROOT).as_posix(),
            }
        else:
            results[name] = {"status": "warning", "reason": "Track path absent"}

    cue_data = read_json(SFX, optional=True)
    cue_count = 0
    if cue_data is not None:
        if not isinstance(cue_data, dict):
            raise RuntimeError("SFX manifest structure गलत है")
        for index, cue in enumerate(rows(cue_data, "cues", "items"), 1):
            if not isinstance(cue, dict):
                raise RuntimeError(f"SFX cue row {index} गलत है")
            try:
                part = int(cue["part"])
                scene = int(cue["scene"])
                timestamp = float(cue.get(
                    "time", cue.get("time_seconds", cue.get("start", 0))
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"SFX cue row {index} गलत है") from exc
            if part < 1 or scene < 1 or not math.isfinite(timestamp) or timestamp < 0:
                raise RuntimeError(f"SFX cue row {index} की timing गलत है")
            path_value = pick(cue, "file", "path", "audio", "output")
            if path_value:
                path = safe_file(path_value, f"SFX cue {index}", 100)
                if not media_info(path)["audio"]:
                    raise RuntimeError(f"SFX cue {index} में audio नहीं")
            cue_count += 1

    return {
        "status": "valid",
        "tracks": results,
        "sfx_cue_count": cue_count,
    }


def validate_render(mode, parts, scenes):
    data = read_json(RENDER)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Render manifest completed नहीं है")
    if str(data.get("format", "")).lower() != mode:
        raise RuntimeError("Render format और input format अलग हैं")
    if data.get("captions_burned") is not True:
        raise RuntimeError("Render manifest burned-in captions की पुष्टि नहीं करता")

    mapping = scene_map(
        rows(data, "completed_scenes", "scenes"),
        parts, scenes, "Render",
    )
    if int(data.get("completed_count", -1)) != len(mapping):
        raise RuntimeError("Render completed_count गलत है")

    scene_results = []
    total_scene_duration = 0.0

    for key, row in mapping.items():
        if row.get("status") != "completed":
            raise RuntimeError(f"Render scene अधूरा है: {key}")
        if row.get("captions_burned") is not True:
            raise RuntimeError(f"Scene पर captions की पुष्टि नहीं: {key}")

        video = safe_file(
            pick(row, "video", "output", "rendered_video"),
            f"Captioned scene {key}", 1000,
        )
        info = require_video(video, f"Captioned scene {key}")
        declared = row.get("duration")
        if declared is not None and abs(float(declared) - info["duration"]) > 1:
            raise RuntimeError(f"Scene duration mismatch: {key}")

        declared_hash = pick(row, "sha256", "video_sha256")
        if declared_hash and declared_hash.lower() != sha256(video):
            raise RuntimeError(f"Scene SHA-256 mismatch: {key}")

        total_scene_duration += info["duration"]
        scene_results.append({
            "part": key[0],
            "scene": key[1],
            "path": video.relative_to(ROOT).as_posix(),
            "sha256": sha256(video),
            "duration": round(info["duration"], 3),
        })

    part_rows = data.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Render manifest में {parts} part videos होने चाहिए")

    part_results = []
    for index, row in enumerate(part_rows, 1):
        if isinstance(row, dict):
            if int(row.get("part", index)) != index:
                raise RuntimeError(f"Part order गलत है: {index}")
            if row.get("status") not in (None, "completed"):
                raise RuntimeError(f"Part completed नहीं है: {index}")
            value = pick(row, "path", "video", "output")
        else:
            value = row

        path = safe_file(value, f"Part video {index}", 1000)
        info = require_video(path, f"Part video {index}")
        part_results.append({
            "part": index,
            "path": path.relative_to(ROOT).as_posix(),
            "duration": round(info["duration"], 3),
            "sha256": sha256(path),
        })

    final = safe_file(data.get("final_video"), "Final video", 1000)
    alias = safe_file(data.get("alias"), "Format alias", 1000)
    final_info = require_video(final, "Final video")
    alias_info = require_video(alias, "Format alias")

    expected_alias = (
        "katha_lok_ai_short.mp4" if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias.name != expected_alias:
        raise RuntimeError(f"Format alias गलत है; अपेक्षित {expected_alias}")
    if abs(final_info["duration"] - alias_info["duration"]) > 1:
        raise RuntimeError("Final video और alias की duration अलग है")

    if abs(final_info["duration"] - sum(p["duration"] for p in part_results)) > max(2, parts):
        raise RuntimeError("Final video duration, parts के कुल समय से मेल नहीं खाती")

    if abs(final_info["duration"] - total_scene_duration) > max(2, len(mapping)):
        raise RuntimeError("Final video duration, scenes के कुल समय से मेल नहीं खाती")

    videos_dir = OUT / "videos"
    opposite = videos_dir / (
        "katha_lok_ai_full.mp4" if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    if opposite.exists():
        raise RuntimeError(f"दूसरे format की stale video मौजूद है: {opposite.name}")

    return {
        "status": "valid",
        "scene_count": len(scene_results),
        "scenes": scene_results,
        "parts": part_results,
        "final_video": final.relative_to(ROOT).as_posix(),
        "final_video_sha256": sha256(final),
        "alias": alias.relative_to(ROOT).as_posix(),
        "alias_sha256": sha256(alias),
        "final_duration_seconds": round(final_info["duration"], 3),
        "resolution": list(RESOLUTION),
    }


def validate_thumbnail(mode):
    path = safe_file("output/thumbnail.jpg", "Thumbnail", 1000)
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dimensions, fmt = image.size, image.format
    except Exception as exc:
        raise RuntimeError(f"Thumbnail image गलत है: {exc}") from exc

    if dimensions != RESOLUTION or fmt != "JPEG":
        raise RuntimeError(
            f"Thumbnail JPEG 1280x720 होना चाहिए; मिला {fmt} {dimensions}"
        )

    manifest = read_json(THUMBNAIL_MANIFEST, optional=True)
    if isinstance(manifest, dict):
        if manifest.get("status") != "completed":
            raise RuntimeError("Thumbnail manifest completed नहीं है")
        if str(manifest.get("format", "")).lower() != mode:
            raise RuntimeError("Thumbnail format mismatch")
        declared = pick(manifest, "thumbnail", "path", "output")
        if declared and safe_file(declared, "Thumbnail manifest").resolve() != path.resolve():
            raise RuntimeError("Thumbnail manifest path mismatch")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "resolution": list(dimensions),
    }


def validate_metadata():
    path = safe_file("output/youtube_metadata.txt", "YouTube metadata", 10)
    content = path.read_text(encoding="utf-8-sig").strip()
    sections = {}
    current = None

    for line in content.splitlines():
        normalized = line.strip().strip("[]").rstrip(":").strip().upper()
        if normalized in {"TITLE", "DESCRIPTION", "TAGS", "LANGUAGE"}:
            current = normalized
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.strip())

    missing = [
        key for key in ("TITLE", "DESCRIPTION", "TAGS")
        if not any(sections.get(key, []))
    ]
    if missing:
        raise RuntimeError("Metadata sections missing/empty: " + ", ".join(missing))

    title = " ".join(sections["TITLE"]).strip()
    description = " ".join(sections["DESCRIPTION"]).strip()
    tags = " ".join(sections["TAGS"]).strip()

    if not title or not description or not tags:
        raise RuntimeError("Metadata title/description/tags खाली हैं")

    return {
        "status": "valid",
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "title_characters": len(title),
        "description_characters": len(description),
        "tags_characters": len(tags),
    }


def main():
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe install नहीं है")

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = str(get_format(config)).strip().lower()
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS और SCENES positive होने चाहिए")

    log("FINAL VALIDATION:", VERSION)
    log("FORMAT:", mode, "| PARTS:", parts, "| SCENES/PART:", scenes)

    checks = {}
    try:
        photo = validate_photo(parts, scenes, mode)
        audio = validate_audio(parts, scenes)
        caption_map = validate_captions(parts, scenes)
        sound = validate_sound()
        render = validate_render(mode, parts, scenes)
        thumbnail = validate_thumbnail(mode)
        metadata = validate_metadata()

        checks = {
            "photo_motion": {"status": "valid", "scene_count": len(photo)},
            "narration": {"status": "valid", "scene_count": len(audio)},
            "captions": {
                "status": "valid",
                "scene_count": len(caption_map),
                "nonempty_text_count": len(caption_map),
            },
            "sound_design": sound,
            "render": {
                key: value for key, value in render.items()
                if key not in {"scenes", "parts"}
            },
            "thumbnail": thumbnail,
            "metadata": metadata,
        }

        report = {
            "status": "completed",
            "version": VERSION,
            "format": mode,
            "parts": parts,
            "scenes_per_part": scenes,
            "expected_scenes": parts * scenes,
            "resolution": {"width": RESOLUTION[0], "height": RESOLUTION[1]},
            "validator_external_api_calls": 0,
            "checks": checks,
            "cross_manifest_validation": {
                "status": "valid",
                "scene_source_mapping_verified": True,
                "scene_narration_mapping_verified": True,
                "verified_scene_count": len(render["scenes"]),
            },
            "render_details": render,
            "limitations": [
                "यह validator दृश्य की perceptual quality का आकलन नहीं करता।",
                "Caption timing की सटीकता या वास्तविक word alignment प्रमाणित नहीं करता।",
                "Manifest में captions flag होना, OCR से हर frame की जाँच के समान नहीं है।",
                "SFX files का होना यह साबित नहीं करता कि final mix में वे पर्याप्त तेज हैं।",
            ],
        }
        write_report(report)
        log("FINAL VALIDATION: PASSED")
        log("VERIFIED SCENES:", len(render["scenes"]))
        log("FINAL VIDEO:", render["final_video"])
        log("REPORT: output/validation_report.json")

    except Exception as exc:
        write_report({
            "status": "failed",
            "version": VERSION,
            "format": mode,
            "parts": parts,
            "scenes_per_part": scenes,
            "expected_scenes": parts * scenes,
            "checks": checks,
            "error": str(exc),
        })
        log("FINAL VALIDATION: FAILED:", str(exc))
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)