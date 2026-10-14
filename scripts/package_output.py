#!/usr/bin/env python3
"""Validate and package Katha Lok AI photo-motion deliverables."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
ARCHIVE = ROOT / "katha-lok-ai-photo-motion-output.zip"
REPORT = OUTPUT / "package_manifest.json"

sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_parts, get_scenes

VERSION = "output-packager-v3-atomic-report"
MIN_VIDEO_BYTES = 1000
MIN_THUMBNAIL_BYTES = 1000


def log(*args):
    print(*args, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_file(path, label, minimum_bytes=1):
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path}")
    if path.stat().st_size < minimum_bytes:
        raise RuntimeError(
            f"{label} too small ({path.stat().st_size} bytes): {path}"
        )
    return path


def read_json(path):
    require_file(path, "JSON manifest")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return data


def safe_output_file(value, label, minimum_bytes=MIN_VIDEO_BYTES):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label}: file path missing")

    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.is_relative_to(OUTPUT.resolve()):
        raise RuntimeError(
            f"{label}: refusing path outside output/: {path}"
        )
    return require_file(path, label, minimum_bytes)


def require_ffprobe():
    import shutil
    if shutil.which("ffprobe") is None:
        raise RuntimeError(
            "ffprobe is unavailable; install FFmpeg before packaging"
        )


def probe_media(path, require_video=False, require_audio=False):
    import subprocess

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"ffprobe failed for {path.name}: "
            f"{result.stderr.strip()[:500]}"
        )

    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError(
            f"Invalid ffprobe output for {path.name}"
        ) from exc

    types = {
        stream.get("codec_type")
        for stream in streams
        if isinstance(stream, dict)
    }
    if require_video and "video" not in types:
        raise RuntimeError(f"No video stream in {path.name}")
    if require_audio and "audio" not in types:
        raise RuntimeError(f"No audio stream in {path.name}")
    return types


def validate_scene_rows(render, parts, scenes):
    expected_count = parts * scenes
    rows = render.get("completed_scenes")

    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} completed scene rows"
        )
    if int(render.get("completed_count", -1)) != expected_count:
        raise RuntimeError("Render manifest completed_count mismatch")

    checked = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Scene row {index} is not an object")

        expected_part = (index - 1) // scenes + 1
        expected_scene = (index - 1) % scenes + 1
        try:
            part = int(row.get("part", 0))
            local_scene = int(row.get("local_scene", row.get("scene", 0)))
            global_scene = int(row.get("global_scene", index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid scene numbering at row {index}"
            ) from exc

        if (part, local_scene, global_scene) != (
            expected_part, expected_scene, index
        ):
            raise RuntimeError(
                f"Scene order mismatch at global scene {index}: "
                f"part={part}, scene={local_scene}, global={global_scene}"
            )

        if row.get("status") != "completed":
            raise RuntimeError(f"Scene {index} is not completed")
        if row.get("captions_burned") is not True:
            raise RuntimeError(
                f"Scene {index} does not confirm burned captions"
            )

        video = safe_output_file(
            row.get("video"), f"Captioned scene {index}"
        )
        probe_media(video, require_video=True, require_audio=True)
        checked.append(video)

    return checked


def validate_sound_design():
    sound_path = OUTPUT / "sound_design" / "sound_design_manifest.json"
    cue_path = OUTPUT / "sound_design" / "sfx_cues.json"

    sound = read_json(sound_path)
    cues = read_json(cue_path)

    if sound.get("status") != "completed":
        raise RuntimeError("Sound-design manifest is not completed")
    if cues.get("status") != "completed":
        raise RuntimeError("SFX cue manifest is not completed")

    tracks = sound.get("tracks", {})
    if not isinstance(tracks, dict):
        raise RuntimeError("Sound-design tracks must be an object")

    for track_name in ("music", "ambient", "sfx"):
        track = tracks.get(track_name)
        if not isinstance(track, dict):
            continue
        if track.get("status") != "completed":
            continue

        value = track.get("path") or track.get("file") or track.get("output")
        if value:
            audio = safe_output_file(
                value, f"Sound-design track {track_name}", 1
            )
            probe_media(audio, require_audio=True)

    cue_rows = cues.get("cues", cues.get("items", []))
    if not isinstance(cue_rows, list):
        raise RuntimeError("SFX cues must be a list")

    for index, cue in enumerate(cue_rows, start=1):
        if not isinstance(cue, dict):
            raise RuntimeError(f"SFX cue {index} is invalid")
        try:
            part = int(cue.get("part", 0))
            scene = int(cue.get("scene", 0))
            start_time = float(cue.get("time", cue.get("start", 0)))
            gain = float(cue.get("gain", 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"SFX cue {index} has invalid numeric values"
            ) from exc

        if part < 1 or scene < 1:
            raise RuntimeError(f"SFX cue {index} has invalid scene mapping")
        if start_time < 0:
            raise RuntimeError(f"SFX cue {index} has negative time")
        if not 0 <= gain <= 4:
            raise RuntimeError(f"SFX cue {index} gain is out of range")

        file_value = cue.get("file") or cue.get("path")
        if file_value:
            audio = safe_output_file(
                file_value, f"SFX cue {index} audio", 1
            )
            probe_media(audio, require_audio=True)

    return {
        "manifest": sound_path,
        "cue_manifest": cue_path,
        "cue_count": len(cue_rows),
    }


def validate_deliverables(mode, parts, scenes):
    require_ffprobe()

    render_path = OUTPUT / "scene_renders" / "render_manifest.json"
    render = read_json(render_path)

    if render.get("status") != "completed":
        raise RuntimeError("Render manifest is not completed")
    if render.get("format") != mode:
        raise RuntimeError("Render manifest format does not match input")
    if render.get("captions_burned") is not True:
        raise RuntimeError("Final render does not confirm burned captions")

    scene_files = validate_scene_rows(render, parts, scenes)

    part_rows = render.get("parts")
    if not isinstance(part_rows, list) or len(part_rows) != parts:
        raise RuntimeError(f"Expected exactly {parts} part videos")

    part_files = []
    for index, value in enumerate(part_rows, start=1):
        video = safe_output_file(value, f"Part video {index}")
        probe_media(video, require_video=True, require_audio=True)
        part_files.append(video)

    final_file = safe_output_file(render.get("final_video"), "Final video")
    probe_media(final_file, require_video=True, require_audio=True)

    alias_file = safe_output_file(render.get("alias"), "Format alias")
    probe_media(alias_file, require_video=True, require_audio=True)

    expected_alias = (
        "katha_lok_ai_short.mp4"
        if mode == "short"
        else "katha_lok_ai_full.mp4"
    )
    if alias_file.name != expected_alias:
        raise RuntimeError(
            f"Expected alias {expected_alias}, got {alias_file.name}"
        )

    opposite_alias = (
        "katha_lok_ai_full.mp4"
        if mode == "short"
        else "katha_lok_ai_short.mp4"
    )
    opposite_path = OUTPUT / "videos" / opposite_alias
    if opposite_path.exists():
        raise RuntimeError(
            f"Opposite-format alias still exists: {opposite_path}"
        )

    thumbnail = require_file(
        OUTPUT / "thumbnail.jpg", "Thumbnail", MIN_THUMBNAIL_BYTES
    )

    metadata_candidates = [
        OUTPUT / "youtube_metadata.txt",
        OUTPUT / "publish" / "youtube_metadata.json",
        OUTPUT / "publish" / "youtube_title.txt",
    ]
    metadata_files = [
        path for path in metadata_candidates
        if path.is_file() and path.stat().st_size >= 10
    ]
    if not metadata_files:
        raise RuntimeError("No usable YouTube metadata file found")

    captions = require_file(
        OUTPUT / "captions" / "final_captions.srt",
        "Final captions SRT", 10,
    )
    srt_text = captions.read_text(encoding="utf-8-sig").strip()
    if "-->" not in srt_text:
        raise RuntimeError("Final SRT contains no cue timing")

    sound = validate_sound_design()

    return {
        "mode": mode,
        "scene_files": scene_files,
        "parts": part_files,
        "final_video": final_file,
        "alias": alias_file,
        "thumbnail": thumbnail,
        "metadata": metadata_files,
        "captions": captions,
        "sound_design": sound,
    }


def excluded_from_archive(path):
    relative = path.relative_to(OUTPUT)

    if path == REPORT:
        # The report contains the final ZIP hash, so including it inside
        # the ZIP would create a self-referential checksum problem.
        return True
    if path.suffix.lower() == ".zip":
        return True
    if path.name.endswith((".tmp", ".tmp.mp4", ".concat.txt", ".lock")):
        return True
    if path.name in {".DS_Store", "Thumbs.db"}:
        return True
    if "__pycache__" in relative.parts:
        return True
    if path.name.endswith(".caption.json"):
        return True
    return False


def build_file_list():
    files = []
    output_root = OUTPUT.resolve()

    for path in OUTPUT.rglob("*"):
        if not path.is_file() or excluded_from_archive(path):
            continue

        resolved = path.resolve()
        if not resolved.is_relative_to(output_root):
            raise RuntimeError(
                f"Refusing output symlink escaping output/: {path}"
            )
        files.append(path)

    return sorted(
        files,
        key=lambda item: item.relative_to(OUTPUT).as_posix(),
    )


def write_archive(files):
    temporary = ARCHIVE.with_name(ARCHIVE.name + ".tmp")
    temporary.unlink(missing_ok=True)
    entries = []

    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in files:
                relative = path.relative_to(OUTPUT).as_posix()
                archive_name = f"output/{relative}"
                archive.write(path, arcname=archive_name)
                entries.append({
                    "path": archive_name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                })

        with zipfile.ZipFile(temporary, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise RuntimeError(f"ZIP integrity failure: {bad_member}")

            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entries detected")
            if any(name.lower().endswith(".zip") for name in names):
                raise RuntimeError("Nested ZIP detected")

            expected_names = {
                f"output/{path.relative_to(OUTPUT).as_posix()}"
                for path in files
            }
            if set(names) != expected_names:
                raise RuntimeError("ZIP entries do not match source file list")

            # Verify every archived file's content against its source hash.
            for path, entry in zip(files, entries):
                archived_bytes = archive.read(entry["path"])
                archived_hash = hashlib.sha256(archived_bytes).hexdigest()
                if archived_hash != entry["sha256"]:
                    raise RuntimeError(
                        f"ZIP content hash mismatch: {entry['path']}"
                    )
                if len(archived_bytes) != entry["bytes"]:
                    raise RuntimeError(
                        f"ZIP file size mismatch: {entry['path']}"
                    )

        temporary.replace(ARCHIVE)
    finally:
        temporary.unlink(missing_ok=True)

    return entries


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = get_format(config)
    parts = int(get_parts(config))
    scenes = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported format: {mode}")
    if parts < 1 or scenes < 1:
        raise RuntimeError("PARTS and SCENES must be positive")
    if not OUTPUT.is_dir():
        raise RuntimeError("output/ directory does not exist")

    deliverables = validate_deliverables(mode, parts, scenes)
    files = build_file_list()
    if not files:
        raise RuntimeError("No files available to package")

    essential = [
        deliverables["final_video"],
        deliverables["alias"],
        deliverables["thumbnail"],
        deliverables["captions"],
        *deliverables["parts"],
        *deliverables["scene_files"],
        *deliverables["metadata"],
    ]
    file_set = {path.resolve() for path in files}
    for path in essential:
        if path.resolve() not in file_set:
            raise RuntimeError(f"Essential deliverable excluded from ZIP: {path}")

    entries = write_archive(files)

    report = {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "parts": parts,
        "scenes_per_part": scenes,
        "scene_count": parts * scenes,
        "archive": ARCHIVE.name,
        "archive_bytes": ARCHIVE.stat().st_size,
        "archive_sha256": sha256_file(ARCHIVE),
        "file_count": len(entries),
        "files": entries,
        "manifest_in_archive": False,
        "manifest_note": (
            "This manifest is intentionally outside the ZIP because it "
            "contains the ZIP's own SHA-256 checksum."
        ),
        "deliverables": {
            "final_video": deliverables["final_video"].relative_to(ROOT).as_posix(),
            "alias": deliverables["alias"].relative_to(ROOT).as_posix(),
            "thumbnail": deliverables["thumbnail"].relative_to(ROOT).as_posix(),
            "captions": deliverables["captions"].relative_to(ROOT).as_posix(),
            "parts": [
                path.relative_to(ROOT).as_posix()
                for path in deliverables["parts"]
            ],
            "metadata": [
                path.relative_to(ROOT).as_posix()
                for path in deliverables["metadata"]
            ],
        },
        "sound_design": {
            "cue_count": deliverables["sound_design"]["cue_count"],
        },
    }

    atomic_write_json(REPORT, report)

    log("PACKAGING COMPLETE")
    log("FORMAT:", mode)
    log("PARTS:", parts)
    log("SCENES:", parts * scenes)
    log("FILES:", len(entries))
    log("ZIP:", ARCHIVE.name)
    log("ZIP BYTES:", ARCHIVE.stat().st_size)
    log("ZIP SHA256:", report["archive_sha256"])
    log("REPORT:", REPORT.relative_to(ROOT))
    log("Manifest included inside ZIP: False")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("PACKAGING FAILED:", str(exc))
        sys.exit(1)