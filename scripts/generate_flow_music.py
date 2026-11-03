#!/usr/bin/env python3
"""Story-aware Google Lyria score with role segments and verified resume."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
AUDIO_JOBS = ROOT / "output" / "narration" / "audio_jobs.json"

AUDIO_DIR = ROOT / "output" / "music"
SOURCE_AUDIO = AUDIO_DIR / "flow_music_source.audio"
FINAL_AUDIO = AUDIO_DIR / "background_music.mp3"
SEGMENT_DIR = AUDIO_DIR / "role_segments"
MANIFEST = AUDIO_DIR / "flow_music_manifest.json"
CHECKPOINT = ROOT / "output" / "checkpoints" / "flow_music_progress.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "lyria-3.5"
VERSION = "photo-motion-lyria-role-segments-v11"
REQUEST_TIMEOUT = (30, 900)
RETRY_DELAYS = (8, 20)
MAX_PROMPT_CHARS = 6500
MIN_AUDIO_BYTES = 10000
MIN_AUDIO_DURATION = 10.0
DURATION_TOLERANCE = 0.65

AUDIO_PATH_KEYS = ("output", "audio_path", "audio_file", "path", "file", "audio")
ROLE_ORDER = {"hook": 0, "main": 1, "suspense": 2, "resolution": 3}
SEGMENT_ROLES = ("hook", "suspense", "resolution")


def log(*values):
    print(*values, flush=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def truthy(value, default=False):
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    raise RuntimeError(f"Invalid boolean setting: {value!r}")


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_settings():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")
    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key.strip().upper()] = value.strip()
    return result


def setting(config, names, default=None):
    for name in names:
        if name in config:
            return config[name]
    return default


def project_path(value):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Audio manifest contains an empty path.")
    path = Path(value.strip())
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    root = ROOT.resolve()
    if path != root and root not in path.parents:
        raise RuntimeError("Audio path escapes the project directory.")
    return path


def run(command, timeout=180):
    result = subprocess.run(
        command, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): "
            f"{(result.stderr or result.stdout or '')[-2000:]}"
        )
    return result


def probe_audio(path, minimum=0.01):
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required.")
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"Audio file is missing: {path}")
    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], 90)
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            row.get("codec_type") == "audio"
            for row in data.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid audio metadata: {path}") from exc
    if not math.isfinite(duration) or duration < minimum or not has_audio:
        raise RuntimeError(f"Audio is empty or invalid: {path}")
    return duration


def measure_audio(path):
    result = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "ebur128=peak=true", "-f", "null", "-",
    ], 180)
    output = result.stderr or ""
    measurements = {}
    patterns = {
        "integrated_lufs": r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS",
        "loudness_range_lu": r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU",
        "true_peak_dbtp": r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS",
    }
    for key, pattern in patterns.items():
        matches = re.findall(pattern, output)
        if matches:
            measurements[key] = float(matches[-1])
    if "integrated_lufs" not in measurements:
        raise RuntimeError("Could not measure integrated music loudness.")
    return measurements


def read_story():
    story = read_json(STORY_FILE)
    if not story:
        raise RuntimeError("output/story/story.json is missing or invalid.")

    title = clean(
        story.get("title") or story.get("final_title")
        or story.get("generated_title") or story.get("topic")
    )
    story_text = clean(
        story.get("story_text") or story.get("final_story")
        or story.get("source_text") or story.get("text")
        or story.get("content")
    )
    summary = clean(
        story.get("summary") or story.get("story_summary")
        or story.get("synopsis") or story.get("description")
    )
    if not title and not story_text and not summary:
        raise RuntimeError("Story title, text and summary are all empty.")
    return title or "Untitled story", story_text, summary


def read_narration_jobs():
    manifest = read_json(AUDIO_JOBS)
    if manifest.get("status") != "completed":
        raise RuntimeError("TTS audio_jobs.json is not completed.")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio_jobs.json contains no jobs.")

    rows, seen = [], set()
    for row in jobs:
        if not isinstance(row, dict) or row.get("status") not in {
            "completed", "reused"
        }:
            raise RuntimeError("TTS manifest contains an incomplete job.")

        scene_id = clean(row.get("scene_id") or row.get("job_id"))
        if not scene_id or scene_id in seen:
            raise RuntimeError(f"Missing or duplicate TTS scene_id: {scene_id}")
        seen.add(scene_id)

        role = clean(row.get("role") or "main").lower()
        if role not in ROLE_ORDER:
            raise RuntimeError(f"Unsupported narration role: {role}")
        try:
            part = int(row["part"])
            scene = int(row["scene"]) if role == "main" else None
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid narration identity: {scene_id}") from exc
        if part < 1 or (role == "main" and scene < 1):
            raise RuntimeError(f"Invalid narration identity: {scene_id}")

        audio_value = next(
            (
                row.get(key) for key in AUDIO_PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        path = project_path(audio_value)
        duration = probe_audio(path)
        checksum = sha256_file(path)
        if row.get("sha256") != checksum:
            raise RuntimeError(f"TTS SHA256 mismatch: {scene_id}")

        declared = row.get(
            "actual_audio_duration_seconds", row.get("duration")
        )
        try:
            declared = float(declared)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Measured duration missing: {scene_id}") from exc
        if not math.isfinite(declared) or abs(declared - duration) > 0.20:
            raise RuntimeError(f"TTS duration mismatch: {scene_id}")
        duration_source = (
            row.get("duration_source") or row.get("audio_duration_source")
        )
        if duration_source != "ffprobe_measured":
            raise RuntimeError(f"Unverified TTS duration: {scene_id}")

        rows.append({
            "scene_id": scene_id, "part": part, "scene": scene,
            "role": role, "duration_seconds": duration,
            "audio_path": path.relative_to(ROOT).as_posix(),
            "audio_sha256": checksum,
            "text": clean(row.get("text") or row.get("narration")),
        })

    rows.sort(key=lambda row: (
        row["part"], ROLE_ORDER[row["role"]],
        row["scene"] or 0, row["scene_id"],
    ))
    return rows


def validate_role_structure(rows, config):
    if not rows:
        raise RuntimeError("No completed narration audio records.")
    parts = sorted({row["part"] for row in rows})
    if not parts or parts != list(range(parts[0], parts[-1] + 1)):
        raise RuntimeError("Narration part numbering is not continuous.")

    try:
        scenes_per_part = int(config.get("SCENES", "1"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SCENES must be a positive integer.") from exc
    if scenes_per_part < 1:
        raise RuntimeError("SCENES must be positive.")

    by_part = {}
    for row in rows:
        by_part.setdefault(row["part"], []).append(row)
    final_part = parts[-1]

    for part in parts:
        group = by_part[part]
        roles = [row["role"] for row in group]
        if roles.count("hook") != 1:
            raise RuntimeError(f"Part {part} must have exactly one Hook.")
        if roles.count("suspense") != (1 if part != final_part else 0):
            raise RuntimeError(f"Part {part} has invalid Suspense records.")
        if roles.count("resolution") != (1 if part == final_part else 0):
            raise RuntimeError(f"Part {part} has invalid Resolution records.")
        main = sorted(row["scene"] for row in group if row["role"] == "main")
        if main != list(range(1, scenes_per_part + 1)):
            raise RuntimeError(f"Part {part} Main scene grid mismatch: {main}")

    expected = (
        len(parts) * scenes_per_part + len(parts)
        + max(0, len(parts) - 1) + 1
    )
    if len(rows) != expected:
        raise RuntimeError(
            f"Narration record count mismatch: expected {expected}, "
            f"found {len(rows)}."
        )
    return {
        "parts": parts, "scenes_per_part": scenes_per_part,
        "main_count": len(parts) * scenes_per_part,
        "hook_count": len(parts),
        "suspense_count": max(0, len(parts) - 1),
        "resolution_count": 1,
    }


def build_timeline(rows):
    offset, timeline = 0.0, []
    for row in rows:
        end = offset + row["duration_seconds"]
        timeline.append({
            "scene_id": row["scene_id"], "part": row["part"],
            "scene": row["scene"], "role": row["role"],
            "start_seconds": round(offset, 4),
            "duration_seconds": round(row["duration_seconds"], 4),
            "end_seconds": round(end, 4),
            "audio_path": row["audio_path"],
            "audio_sha256": row["audio_sha256"],
        })
        offset = end
    return timeline, round(offset, 4)


def story_context(story_text, summary):
    context = clean((summary + " " + story_text).strip())
    if len(context) <= MAX_PROMPT_CHARS:
        return context
    limit = MAX_PROMPT_CHARS - 500
    first = int(limit * 0.42)
    last = int(limit * 0.30)
    middle = limit - first - last
    start = max(0, (len(context) - middle) // 2)
    return clean(
        context[:first] + " [middle story events] "
        + context[start:start + middle]
        + " [complete ending and resolution] " + context[-last:]
    )


def build_prompt(title, story_text, summary, rows, config):
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"
    style = clean(config.get("MUSIC_STYLE", "cinematic")) or "cinematic"
    genre = clean(config.get("MUSIC_GENRE", "original film score"))
    intensity = clean(config.get("MUSIC_INTENSITY", "evolving"))
    role_sections = []
    for role in ("hook", "main", "suspense", "resolution"):
        samples = [r["text"] for r in rows if r["role"] == role and r["text"]]
        if samples:
            role_sections.append(
                f"{role.upper()} narration: {' '.join(samples[:8])[:1000]}"
            )

    return (
        "Create one original, instrumental cinematic score for this Hindi "
        "storytelling video. Do not copy or imitate existing songs, artists "
        "or soundtracks. Understand the complete story, including the final "
        "reveal and resolution. Use one coherent recurring musical motif "
        "across all parts. The Hook begins restrained but intriguing. Main "
        "sections support narration without masking speech. Suspense sections "
        "build tension without sudden loud peaks. The final Resolution reaches "
        "a satisfying emotional landing and ends naturally. The music must "
        "reflect the actual characters, locations, events and emotional "
        "changes, not generic trailer music. Instrumental only, no vocals, "
        "lyrics or spoken words. Keep the speech midrange clear, bass "
        "controlled, transitions smooth and motif recognizable. "
        f"Story title: {title}. Style: {style}. Genre: {genre}. "
        f"Mood: {mood}. Intensity: {intensity}. "
        f"Full story context: {story_context(story_text, summary)}\n"
        + "\n".join(role_sections)
    )


def decode_audio_block(block):
    if not isinstance(block, dict):
        return None
    encoded = block.get("data")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None


def extract_audio(data):
    if not isinstance(data, dict):
        raise RuntimeError("Google API returned a non-object response.")
    candidates = []
    if isinstance(data.get("output_audio"), dict):
        candidates.append(data["output_audio"])
    for key in ("outputs", "output", "steps"):
        items = data.get(key, [])
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"audio", "output_audio"}:
                candidates.append(item)
            content = item.get("content", [])
            if isinstance(content, dict):
                content = [content]
            if isinstance(content, list):
                candidates.extend(
                    block for block in content
                    if isinstance(block, dict)
                    and block.get("type") in {"audio", "output_audio"}
                )
    for block in candidates:
        audio = decode_audio_block(block)
        if audio and len(audio) >= MIN_AUDIO_BYTES:
            return audio
    raise RuntimeError("Google response contains no valid audio data block.")


def request_music(api_key, model, prompt):
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {"model": model, "input": prompt}
    last_error = "Unknown Google Lyria API error."

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = requests.post(
                API_URL, headers=headers, json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code in {408, 429, 500, 502, 503, 504}:
                last_error = (
                    f"Temporary Google API error {response.status_code}: "
                    f"{response.text[:800]}"
                )
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                break
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Google API rejected request ({response.status_code}): "
                    f"{response.text[:1200]}"
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("Google API returned invalid JSON.") from exc
            audio = extract_audio(data)
            temporary = SOURCE_AUDIO.with_name(SOURCE_AUDIO.name + ".tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            try:
                temporary.write_bytes(audio)
                probe_audio(temporary, MIN_AUDIO_DURATION)
                os.replace(temporary, SOURCE_AUDIO)
            finally:
                temporary.unlink(missing_ok=True)
            return probe_audio(SOURCE_AUDIO, MIN_AUDIO_DURATION)
        except requests.Timeout as exc:
            last_error = f"Google music request timed out: {exc}"
        except requests.ConnectionError as exc:
            last_error = f"Google music connection failed: {exc}"
        except requests.RequestException as exc:
            raise RuntimeError(f"Google music request failed: {exc}") from exc
        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
    raise RuntimeError(last_error)


def validate_model(config):
    model = clean(
        config.get("FLOW_MUSIC_MODEL")
        or os.environ.get("FLOW_MUSIC_MODEL")
        or DEFAULT_MODEL
    )
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        raise RuntimeError("FLOW_MUSIC_MODEL is invalid.")
    return model


def source_fingerprint(title, story_text, summary, rows, config,
                       model, target_duration, prompt):
    return fingerprint({
        "version": VERSION, "provider": "google_lyria", "model": model,
        "title": title, "story_text": story_text, "summary": summary,
        "narration": [
            {
                "scene_id": row["scene_id"], "part": row["part"],
                "scene": row["scene"], "role": row["role"],
                "duration": round(row["duration_seconds"], 4),
                "audio_sha256": row["audio_sha256"],
            } for row in rows
        ],
        "settings": {
            key: config.get(key) for key in (
                "MUSIC", "MUSIC_ENABLED", "MUSIC_STYLE", "MUSIC_GENRE",
                "MUSIC_INTENSITY", "MOOD", "FLOW_MUSIC_MODEL",
                "MUSIC_ALLOW_BILLABLE", "ALLOW_BILLABLE_PROVIDERS",
            )
        },
        "target_duration": round(target_duration, 3),
        "prompt": prompt,
    })


def billable_allowed(config):
    global_permission = truthy(
        os.environ.get(
            "ALLOW_BILLABLE_PROVIDERS",
            config.get("ALLOW_BILLABLE_PROVIDERS", "true"),
        ), True,
    )
    music_permission = truthy(
        config.get("MUSIC_ALLOW_BILLABLE", "true"), True
    )
    return global_permission and music_permission


def prepare_master(source, destination, duration):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required.")
    source_duration = probe_audio(source, MIN_AUDIO_DURATION)
    fade = min(5.0, max(1.0, duration * 0.04))
    fade_start = max(0.0, duration - fade)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)
    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(source),
            "-t", f"{duration:.3f}", "-vn",
            "-af",
            (
                "aresample=44100,"
                "loudnorm=I=-28:TP=-3.0:LRA=10,"
                f"afade=t=out:st={fade_start:.3f}:d={fade:.3f}"
            ),
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-map_metadata", "-1", str(temporary),
        ], 900)
        actual = probe_audio(temporary, MIN_AUDIO_DURATION)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Music duration mismatch: {actual:.3f}s vs {duration:.3f}s."
            )
        os.replace(temporary, destination)
        return {
            "duration_seconds": actual,
            "source_duration_seconds": source_duration,
            "sha256": sha256_file(destination),
            "size_bytes": destination.stat().st_size,
            "loudness": measure_audio(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)


def extract_segment(master, destination, start, duration):
    if duration <= 0.05:
        raise RuntimeError("Music segment duration is invalid.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)
    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.4f}", "-i", str(master),
            "-t", f"{duration:.4f}", "-vn",
            "-af", "afade=t=in:d=0.35,afade=t=out:st="
                   f"{max(0.0, duration - 0.65):.4f}:d=0.65",
            "-c:a", "libmp3lame", "-b:a", "160k",
            "-ar", "44100", "-ac", "2", str(temporary),
        ], 300)
        actual = probe_audio(temporary, 0.05)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Role segment duration mismatch: {actual:.3f}s vs "
                f"{duration:.3f}s."
            )
        os.replace(temporary, destination)
        return {
            "path": destination.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(destination),
            "duration_seconds": round(actual, 4),
            "start_seconds": round(start, 4),
            "loudness": measure_audio(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)


def build_role_segments(master, timeline):
    segments = []
    grouped = {role: [] for role in SEGMENT_ROLES}
    for row in timeline:
        role = row["role"]
        if role not in SEGMENT_ROLES:
            continue
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", row["scene_id"]).strip("_")
        target = SEGMENT_DIR / f"{name}.mp3"
        item = extract_segment(
            master, target, row["start_seconds"], row["duration_seconds"]
        )
        item.update({
            "scene_id": row["scene_id"], "part": row["part"],
            "role": role, "scene": row["scene"],
            "narration_sha256": row["audio_sha256"],
            "segment_type": "narration_aligned_role_excerpt",
        })
        segments.append(item)
        grouped[role].append(item)

    role_summary = {}
    for role, items in grouped.items():
        role_summary[role] = {
            "count": len(items),
            "segments": items,
            "total_duration_seconds": round(
                sum(item["duration_seconds"] for item in items), 4
            ),
        }
    return segments, role_summary


def write_unavailable(reason, model=None, duration=None, signature=None):
    record = {
        "status": "unavailable", "provider": "google_lyria",
        "product": "Google Lyria", "model": model or DEFAULT_MODEL,
        "reason": str(reason)[:2000],
        "fallback": "sound_design_local_fallback",
        "paid_services_used": False, "external_api_calls": 0,
        "version": VERSION,
    }
    if duration is not None:
        record["required_duration_seconds"] = round(float(duration), 3)
    if signature:
        record["source_fingerprint"] = signature
    write_json(MANIFEST, record)
    write_json(CHECKPOINT, {
        "status": "unavailable", "version": VERSION,
        "reason": str(reason)[:2000],
    })


def write_disabled():
    write_json(MANIFEST, {
        "status": "disabled", "provider": "google_lyria",
        "reason": "Background music is disabled in Input/topic.txt.",
        "paid_services_used": False, "external_api_calls": 0,
        "version": VERSION,
    })
    write_json(CHECKPOINT, {"status": "disabled", "version": VERSION})


def get_api_key():
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def verify_existing(signature, duration):
    previous = read_json(MANIFEST)
    if (
        previous.get("status") != "completed"
        or previous.get("version") != VERSION
        or previous.get("source_fingerprint") != signature
        or previous.get("provider") != "google_lyria"
    ):
        return False
    try:
        path = project_path(previous.get("path"))
        if path != FINAL_AUDIO.resolve():
            return False
        actual = probe_audio(path, MIN_AUDIO_DURATION)
        if previous.get("sha256") != sha256_file(path):
            return False
        if abs(actual - duration) > DURATION_TOLERANCE:
            return False
        segments = previous.get("role_segments")
        if not isinstance(segments, list):
            return False
        for item in segments:
            segment_path = project_path(item.get("path"))
            if item.get("sha256") != sha256_file(segment_path):
                return False
            if abs(
                probe_audio(segment_path, 0.05)
                - float(item["duration_seconds"])
            ) > DURATION_TOLERANCE:
                return False
        log("RESUME: verified master music and role segments reused.")
        return True
    except Exception:
        return False

API_CALLS = 0


def main():
    global API_CALLS

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    config = read_settings()

    enabled = truthy(
        setting(
            config,
            ("MUSIC_ENABLED", "MUSIC", "BACKGROUND_MUSIC"),
            "true",
        ),
        True,
    )
    if not enabled:
        write_disabled()
        log("MUSIC DISABLED.")
        return 0

    model = validate_model(config)
    if not billable_allowed(config):
        write_unavailable(
            "Billable music generation is disabled by configuration.",
            model=model,
        )
        log("LYRIA SKIPPED: billable permission is disabled.")
        return 0

    api_key = get_api_key()
    if not api_key:
        write_unavailable(
            "GEMINI_API_KEY and GOOGLE_API_KEY are not configured.",
            model=model,
        )
        log("LYRIA UNAVAILABLE: Google API key is missing.")
        return 0

    target_duration = None
    source_sig = None

    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("ffmpeg and ffprobe are required.")

        title, story_text, summary = read_story()
        rows = read_narration_jobs()
        role_summary = validate_role_structure(rows, config)

        narration_duration = sum(
            row["duration_seconds"] for row in rows
        )
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            raise RuntimeError("Measured narration duration is invalid.")

        target_duration = round(narration_duration + 3.0, 3)
        timeline, timeline_duration = build_timeline(rows)
        if abs(timeline_duration - narration_duration) > 0.05:
            raise RuntimeError("Narration timeline duration mismatch.")

        prompt = build_prompt(
            title, story_text, summary, rows, config
        )
        source_sig = source_fingerprint(
            title, story_text, summary, rows, config,
            model, target_duration, prompt,
        )

        if verify_existing(source_sig, target_duration):
            write_json(CHECKPOINT, {
                "status": "completed",
                "version": VERSION,
                "source_fingerprint": source_sig,
                "reused": True,
                "api_calls": 0,
                "paid_services_used": False,
                "updated_at": time.time(),
            })
            return 0

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "target_duration_seconds": target_duration,
            "narration_records": len(rows),
            "role_summary": role_summary,
            "api_calls_started": 0,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("GOOGLE LYRIA ROLE-AWARE MUSIC")
        log("MODEL:", model)
        log("STORY:", title)
        log("PARTS:", len(role_summary["parts"]))
        log("NARRATION RECORDS:", len(rows))
        log("MEASURED NARRATION:", round(narration_duration, 2))
        log("REQUIRED MUSIC:", target_duration)
        log("ROLE COUNTS:", role_summary)
        log("=" * 60)

        source_duration = request_music(api_key, model, prompt)

        prepared = prepare_master(
            SOURCE_AUDIO, FINAL_AUDIO, target_duration
        )
        if prepared["sha256"] != sha256_file(FINAL_AUDIO):
            raise RuntimeError("Master music SHA256 verification failed.")

        verified_duration = probe_audio(FINAL_AUDIO, MIN_AUDIO_DURATION)
        if abs(verified_duration - target_duration) > DURATION_TOLERANCE:
            raise RuntimeError("Master music duration verification failed.")

        role_segments, role_segment_summary = build_role_segments(
            FINAL_AUDIO, timeline
        )

        required_roles = {
            "hook": role_summary["hook_count"],
            "suspense": role_summary["suspense_count"],
            "resolution": role_summary["resolution_count"],
        }
        for role, expected_count in required_roles.items():
            actual_count = sum(
                1 for item in role_segments if item["role"] == role
            )
            if actual_count != expected_count:
                raise RuntimeError(
                    f"{role} music segment count mismatch: "
                    f"expected {expected_count}, found {actual_count}."
                )

        # Validate every role excerpt against its measured narration record.
        narration_by_id = {row["scene_id"]: row for row in rows}
        for item in role_segments:
            narration = narration_by_id.get(item["scene_id"])
            if narration is None:
                raise RuntimeError(
                    f"Music segment has no narration record: {item['scene_id']}"
                )
            if item["narration_sha256"] != narration["audio_sha256"]:
                raise RuntimeError(
                    f"Music segment narration hash mismatch: {item['scene_id']}"
                )
            if abs(
                item["duration_seconds"] - narration["duration_seconds"]
            ) > DURATION_TOLERANCE:
                raise RuntimeError(
                    f"Music segment is not aligned to narration: "
                    f"{item['scene_id']}"
                )

        master_loudness = measure_audio(FINAL_AUDIO)
        if master_loudness.get("true_peak_dbtp", 0) > -1.0:
            raise RuntimeError("Master music true peak exceeds safe limit.")
        if master_loudness["integrated_lufs"] > -20.0:
            raise RuntimeError("Master music is too loud for narration headroom.")

        source_checksum = sha256_file(SOURCE_AUDIO)
        final_checksum = sha256_file(FINAL_AUDIO)

        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Lyria",
            "model": model,
            "generation_method": "google_interactions_api",
            "source_audio": SOURCE_AUDIO.relative_to(ROOT).as_posix(),
            "path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "audio_path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "duration_seconds": round(verified_duration, 3),
            "actual_audio_duration_seconds": round(verified_duration, 3),
            "required_duration_seconds": target_duration,
            "source_duration_seconds": round(source_duration, 3),
            "sha256": final_checksum,
            "source_sha256": source_checksum,
            "source_fingerprint": source_sig,
            "instrumental_only": True,
            "lyrics_requested": False,
            "looped_to_narration_duration": (
                target_duration > source_duration + 0.5
            ),
            "fade_out_enabled": True,
            "narration_duration_source": (
                "sum_of_ffprobe_measured_tts_audio_files"
            ),
            "narration_records": len(rows),
            "role_summary": role_summary,
            "role_segments": role_segments,
            "role_segment_summary": role_segment_summary,
            "role_segment_directory": (
                SEGMENT_DIR.relative_to(ROOT).as_posix()
            ),
            "role_segment_policy": (
                "One narration-aligned music excerpt for every Hook, "
                "Suspense and Resolution record. All excerpts originate "
                "from the same generated score and preserve its motif."
            ),
            "story_aware_prompt": True,
            "story_beginning_considered": True,
            "story_middle_considered": True,
            "story_ending_considered": True,
            "common_musical_identity": True,
            "narration_headroom": {
                "target_integrated_lufs": -28.0,
                "true_peak_limit_dbtp": -3.0,
                "measured": master_loudness,
                "speech_midrange_preserved": True,
                "final_ducking_applied_by": "render_video.py",
            },
            "master_loudness": master_loudness,
            "segment_loudness": {
                item["scene_id"]: item["loudness"]
                for item in role_segments
            },
            "timeline": timeline,
            "external_api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "size_bytes": FINAL_AUDIO.stat().st_size,
            "version": VERSION,
            "completed_at": time.time(),
        }

        write_json(MANIFEST, record)

        published = read_json(MANIFEST)
        if (
            published.get("status") != "completed"
            or published.get("sha256") != final_checksum
            or published.get("source_fingerprint") != source_sig
            or published.get("path") != FINAL_AUDIO.relative_to(ROOT).as_posix()
            or len(published.get("role_segments", [])) != len(role_segments)
        ):
            raise RuntimeError(
                "Published Flow Music manifest failed verification."
            )

        # Verify persisted segment files and hashes after manifest write.
        for item in published["role_segments"]:
            path = project_path(item["path"])
            if not path.is_file() or sha256_file(path) != item["sha256"]:
                raise RuntimeError(
                    f"Published role segment failed SHA256: {item['scene_id']}"
                )
            actual = probe_audio(path, 0.05)
            if abs(actual - item["duration_seconds"]) > DURATION_TOLERANCE:
                raise RuntimeError(
                    f"Published role segment duration mismatch: "
                    f"{item['scene_id']}"
                )

        write_json(CHECKPOINT, {
            "status": "completed",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "sha256": final_checksum,
            "duration_seconds": round(verified_duration, 3),
            "target_duration_seconds": target_duration,
            "role_segment_count": len(role_segments),
            "role_segment_sha256": {
                item["scene_id"]: item["sha256"]
                for item in role_segments
            },
            "api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("LYRIA MUSIC READY.")
        log("MODEL:", model)
        log("SOURCE DURATION:", round(source_duration, 2))
        log("FINAL DURATION:", round(verified_duration, 2))
        log("HOOK SEGMENTS:", required_roles["hook"])
        log("SUSPENSE SEGMENTS:", required_roles["suspense"])
        log("RESOLUTION SEGMENTS:", required_roles["resolution"])
        log("INTEGRATED LOUDNESS:", master_loudness["integrated_lufs"])
        log("TRUE PEAK:", master_loudness.get("true_peak_dbtp"))
        log("API CALLS:", API_CALLS)
        log("OUTPUT:", FINAL_AUDIO.relative_to(ROOT))
        log("MANIFEST:", MANIFEST.relative_to(ROOT))
        log("=" * 60)
        return 0

    except KeyboardInterrupt:
        write_json(CHECKPOINT, {
            "status": "interrupted",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "api_calls": API_CALLS,
            "updated_at": time.time(),
        })
        raise

    except Exception as exc:
        write_unavailable(
            reason=exc,
            model=model,
            duration=target_duration,
            signature=source_sig,
        )
        failure = read_json(MANIFEST)
        failure["external_api_calls"] = API_CALLS
        failure["paid_services_used"] = API_CALLS > 0
        write_json(MANIFEST, failure)
        log("LYRIA MUSIC UNAVAILABLE.")
        log("LOCAL SOUND-DESIGN FALLBACK MAY BE USED.")
        log("API CALLS:", API_CALLS)
        log("REASON:", str(exc)[:1500])
        log("NO FALSE COMPLETION RECORDED.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        raise SystemExit(130)