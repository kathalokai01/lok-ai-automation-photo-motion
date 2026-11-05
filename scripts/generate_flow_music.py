#!/usr/bin/env python3
"""Story-aware Google Lyria music with duration, loudness and resume validation."""

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
SOURCE_DIR = AUDIO_DIR / "role_sources"
SEGMENT_DIR = AUDIO_DIR / "role_segments"
FINAL_AUDIO = AUDIO_DIR / "background_music.mp3"
MANIFEST = AUDIO_DIR / "flow_music_manifest.json"
CHECKPOINT = ROOT / "output" / "checkpoints" / "flow_music_progress.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "lyria-3.5"
VERSION = "photo-motion-lyria-duration-volume-resume-v14"

REQUEST_TIMEOUT = (30, 900)
RETRY_DELAYS = (8, 20)
MIN_AUDIO_BYTES = 10000
MAX_AUDIO_BYTES = 100 * 1024 * 1024
MIN_AUDIO_DURATION = 10.0
DURATION_TOLERANCE = 0.65
MUSIC_TARGET_LUFS = -22.0
MUSIC_TRUE_PEAK_LIMIT = -3.0
SAFE_MASTER_PEAK = -1.0
MASTER_TAIL_SECONDS = 3.0

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
}
SEGMENT_ROLES = ("hook", "main", "suspense", "resolution")
AUDIO_PATH_KEYS = ("output", "audio_path", "audio_file", "path", "file", "audio")

API_CALLS = 0
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KathaLokAI-PhotoMotion/14.0"})


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
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        result[key.strip().upper()] = value.strip()
    return result


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
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
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
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def run(command, timeout=300):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): "
            f"{(result.stderr or result.stdout or '')[-2000:]}"
        )
    return result


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


def probe_audio(path, minimum=0.01):
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required.")

    path = Path(path)
    if (
        not path.is_file()
        or path.stat().st_size < MIN_AUDIO_BYTES
        or path.stat().st_size > MAX_AUDIO_BYTES
    ):
        raise RuntimeError(f"Audio is missing, empty or oversized: {path}")

    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ], 90)

    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        has_audio = any(
            item.get("codec_type") == "audio"
            for item in data.get("streams", [])
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Invalid audio metadata: {path}") from exc

    if not math.isfinite(duration) or duration < minimum or not has_audio:
        raise RuntimeError(f"Audio is invalid or too short: {path}")
    return duration


def measure_loudness(path):
    result = run([
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", "ebur128=peak=true",
        "-f", "null", "-",
    ], 240)

    output = result.stderr or ""
    patterns = {
        "integrated_lufs": r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS",
        "loudness_range_lu": r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU",
        "true_peak_dbtp": r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS",
    }
    values = {}
    for key, pattern in patterns.items():
        found = re.findall(pattern, output)
        if found:
            values[key] = float(found[-1])

    if "integrated_lufs" not in values or "true_peak_dbtp" not in values:
        raise RuntimeError("Music loudness measurement is incomplete.")
    return values


def validate_master_loudness(values):
    integrated = values.get("integrated_lufs")
    peak = values.get("true_peak_dbtp")
    if integrated is None or peak is None:
        raise RuntimeError("Master music loudness/peak data is missing.")
    if integrated > -20.0:
        raise RuntimeError(
            f"Music is too loud for narration headroom: {integrated} LUFS."
        )
    if peak > SAFE_MASTER_PEAK:
        raise RuntimeError(
            f"Music true peak exceeds safe limit: {peak} dBFS."
        )


def read_story():
    story = read_json(STORY_FILE)
    if not story:
        raise RuntimeError("output/story/story.json is missing or invalid.")

    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("generated_title")
        or story.get("topic")
    )
    story_text = clean(
        story.get("corrected_complete_story")
        or story.get("story_text")
        or story.get("final_story")
        or story.get("source_text")
        or story.get("text")
        or story.get("content")
    )
    summary = clean(
        story.get("summary")
        or story.get("story_summary")
        or story.get("synopsis")
        or story.get("description")
    )

    if not story_text and not summary:
        raise RuntimeError("Complete story text or summary is required.")
    return title or "Untitled story", story_text, summary


def parse_scenes_range(config):
    raw = clean(config.get("SCENES_SPEC") or config.get("SCENES") or "1")
    match = re.fullmatch(r"(\d+)\s*(?:-\s*(\d+))?", raw)
    if not match:
        raise RuntimeError(f"Invalid SCENES range: {raw!r}")

    minimum = int(match.group(1))
    maximum = int(match.group(2) or match.group(1))
    if minimum < 1 or maximum < minimum or maximum > 500:
        raise RuntimeError(f"Invalid SCENES range: {raw!r}")

    supplied_min = config.get("SCENES_MIN")
    supplied_max = config.get("SCENES_MAX")
    if supplied_min not in (None, "") and supplied_max not in (None, ""):
        try:
            supplied = (int(supplied_min), int(supplied_max))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("SCENES_MIN/MAX must be integers.") from exc
        if supplied != (minimum, maximum):
            raise RuntimeError("SCENES range settings do not match.")
    return minimum, maximum


def read_narration_jobs():
    manifest = read_json(AUDIO_JOBS)
    if manifest.get("status") != "completed":
        raise RuntimeError("TTS audio_jobs.json is not completed.")

    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("TTS audio_jobs.json contains no jobs.")

    rows = []
    seen = set()

    for row in jobs:
        if (
            not isinstance(row, dict)
            or row.get("status") not in {"completed", "reused"}
        ):
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

        if part < 1 or (role == "main" and (scene is None or scene < 1)):
            raise RuntimeError(f"Invalid narration identity: {scene_id}")

        audio_value = next(
            (
                row.get(key)
                for key in AUDIO_PATH_KEYS
                if isinstance(row.get(key), str) and row[key].strip()
            ),
            None,
        )
        path = project_path(audio_value)
        duration = probe_audio(path)
        checksum = sha256_file(path)

        if clean(row.get("sha256")).lower() != checksum.lower():
            raise RuntimeError(f"TTS SHA256 mismatch: {scene_id}")

        declared = row.get(
            "actual_audio_duration_seconds",
            row.get("duration"),
        )
        try:
            declared = float(declared)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Measured duration missing: {scene_id}") from exc

        if not math.isfinite(declared) or abs(declared - duration) > 0.20:
            raise RuntimeError(f"TTS duration mismatch: {scene_id}")

        duration_source = (
            row.get("duration_source")
            or row.get("audio_duration_source")
        )
        if duration_source != "ffprobe_measured":
            raise RuntimeError(f"Unverified TTS duration: {scene_id}")

        rows.append({
            "scene_id": scene_id,
            "part": part,
            "scene": scene,
            "role": role,
            "duration_seconds": duration,
            "audio_path": path.relative_to(ROOT).as_posix(),
            "audio_sha256": checksum,
            "text": clean(row.get("text") or row.get("narration")),
        })

    rows.sort(key=lambda item: (
        item["part"],
        ROLE_ORDER[item["role"]],
        item["scene"] or 0,
        item["scene_id"],
    ))
    return rows


def validate_role_structure(rows, config):
    if not rows:
        raise RuntimeError("No completed narration records.")

    parts = sorted({row["part"] for row in rows})
    if not parts or parts != list(range(parts[0], parts[-1] + 1)):
        raise RuntimeError("Narration Part numbering is not continuous.")

    scenes_min, scenes_max = parse_scenes_range(config)
    by_part = {}
    for row in rows:
        by_part.setdefault(row["part"], []).append(row)

    actual_counts = {}
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
        if (
            len(main) < scenes_min
            or len(main) > scenes_max
            or main != list(range(1, len(main) + 1))
        ):
            raise RuntimeError(
                f"Part {part} Main Scenes are outside "
                f"{scenes_min}-{scenes_max} or are not continuous."
            )
        actual_counts[str(part)] = len(main)

    expected = (
        sum(actual_counts.values())
        + len(parts)
        + max(0, len(parts) - 1)
        + 1
    )
    if len(rows) != expected:
        raise RuntimeError(
            f"Narration record count mismatch: expected {expected}, "
            f"found {len(rows)}."
        )

    return {
        "parts": parts,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "actual_scene_counts": actual_counts,
        "main_count": sum(actual_counts.values()),
        "hook_count": len(parts),
        "suspense_count": max(0, len(parts) - 1),
        "resolution_count": 1,
    }


def build_timeline(rows):
    offset = 0.0
    timeline = []
    for row in rows:
        duration = row["duration_seconds"]
        end = offset + duration
        timeline.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "start_seconds": round(offset, 4),
            "duration_seconds": round(duration, 4),
            "end_seconds": round(end, 4),
            "audio_path": row["audio_path"],
            "audio_sha256": row["audio_sha256"],
        })
        offset = end
    return timeline, round(offset, 4)


def story_context(story_text, summary):
    value = clean((summary + " " + story_text).strip())
    if len(value) <= 6000:
        return value

    first = value[:2200]
    middle_start = max(0, (len(value) - 1800) // 2)
    middle = value[middle_start:middle_start + 1800]
    ending = value[-1800:]
    return clean(
        first + " [middle story events] " + middle
        + " [complete ending and resolution] " + ending
    )


def build_role_prompt(title, story_text, summary, role, rows, config):
    role_rows = [row for row in rows if row["role"] == role]
    duration = sum(row["duration_seconds"] for row in role_rows)
    narration = clean(" ".join(row["text"] for row in role_rows if row["text"]))
    narration = narration[:1800]

    directions = {
        "hook": (
            "Create an intriguing opening with immediate emotional interest, "
            "restrained mystery and a memorable musical motif."
        ),
        "main": (
            "Create sustained narrative underscore that evolves with the "
            "story and leaves the speech midrange clear."
        ),
        "suspense": (
            "Create a distinct tension-building composition that increases "
            "anticipation gradually without harsh peaks."
        ),
        "resolution": (
            "Create an emotionally satisfying resolution with a clear "
            "musical landing and natural finish."
        ),
    }

    style = clean(config.get("MUSIC_STYLE", "cinematic")) or "cinematic"
    genre = clean(config.get("MUSIC_GENRE", "original film score"))
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"
    intensity = clean(config.get("MUSIC_INTENSITY", "evolving"))

    return (
        "Create an original instrumental cinematic composition for a Hindi "
        "storytelling video. This must be an independent composition for "
        f"the {role.upper()} role, not an excerpt from another role. "
        "No vocals, lyrics, speech or chanting. Do not imitate existing "
        "songs, artists or soundtracks. Avoid repetitive short loops, abrupt "
        "cuts and long silent gaps. Keep bass controlled and speech midrange "
        "clear. "
        f"{directions[role]} "
        f"Story title: {title}. Style: {style}. Genre: {genre}. "
        f"Mood: {mood}. Intensity: {intensity}. "
        f"Required role duration: {duration:.2f} seconds. "
        f"Story context: {story_context(story_text, summary)}. "
        f"{role.upper()} narration context: {narration}"
    )


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
        encoded = block.get("data")
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        if MIN_AUDIO_BYTES <= len(audio) <= MAX_AUDIO_BYTES:
            return audio

    raise RuntimeError("Google response contains no valid audio data.")


def request_music(api_key, model, prompt, destination):
    global API_CALLS

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {"model": model, "input": prompt}
    last_error = "Unknown Google Lyria API error."

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            API_CALLS += 1
            response = SESSION.post(
                API_URL,
                headers=headers,
                json=payload,
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
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            try:
                temporary.write_bytes(audio)
                probe_audio(temporary, MIN_AUDIO_DURATION)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

            return probe_audio(destination, MIN_AUDIO_DURATION)

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
    if (
        not model
        or len(model) > 200
        or any(character.isspace() for character in model)
    ):
        raise RuntimeError("FLOW_MUSIC_MODEL is invalid.")
    return model


def billable_allowed(config):
    global_permission = truthy(
        os.environ.get(
            "ALLOW_BILLABLE_PROVIDERS",
            config.get("ALLOW_BILLABLE_PROVIDERS", "true"),
        ),
        True,
    )
    music_permission = truthy(config.get("MUSIC_ALLOW_BILLABLE", "true"), True)
    return global_permission and music_permission


def get_api_key():
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def source_fingerprint(
    title, story_text, summary, rows, config, model,
    target_duration, prompts, role_durations,
):
    return fingerprint({
        "version": VERSION,
        "provider": "google_lyria",
        "model": model,
        "title": title,
        "story_text": story_text,
        "summary": summary,
        "narration": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"],
                "scene": row["scene"],
                "role": row["role"],
                "duration": round(row["duration_seconds"], 4),
                "audio_sha256": row["audio_sha256"],
            }
            for row in rows
        ],
        "settings": {
            key: config.get(key)
            for key in (
                "SCENES_SPEC", "SCENES_MIN", "SCENES_MAX",
                "MUSIC", "MUSIC_ENABLED", "MUSIC_STYLE",
                "MUSIC_GENRE", "MUSIC_INTENSITY", "MOOD",
                "FLOW_MUSIC_MODEL", "MUSIC_ALLOW_BILLABLE",
                "ALLOW_BILLABLE_PROVIDERS",
            )
        },
        "role_durations": role_durations,
        "target_duration": round(target_duration, 3),
        "prompts": prompts,
        "target_lufs": MUSIC_TARGET_LUFS,
        "true_peak_limit": MUSIC_TRUE_PEAK_LIMIT,
    })


def prepare_composition(source, destination, duration):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required.")

    source_duration = probe_audio(source, MIN_AUDIO_DURATION)
    target = max(MIN_AUDIO_DURATION, duration)
    fade = min(2.0, max(0.5, target * 0.03))
    fade_start = max(0.0, target - fade)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(source),
            "-t", f"{target:.3f}", "-vn",
            "-af",
            (
                "aresample=44100,"
                f"loudnorm=I={MUSIC_TARGET_LUFS}:"
                f"TP={MUSIC_TRUE_PEAK_LIMIT}:LRA=10,"
                f"afade=t=out:st={fade_start:.3f}:d={fade:.3f}"
            ),
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-map_metadata", "-1", str(temporary),
        ], 900)

        actual = probe_audio(temporary, MIN_AUDIO_DURATION)
        if abs(actual - target) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Role composition duration mismatch: {actual:.3f}s "
                f"vs {target:.3f}s."
            )

        loudness = measure_loudness(temporary)
        if loudness["true_peak_dbtp"] > SAFE_MASTER_PEAK:
            raise RuntimeError("Role composition true peak is unsafe.")

        os.replace(temporary, destination)
        return {
            "duration_seconds": actual,
            "source_duration_seconds": source_duration,
            "sha256": sha256_file(destination),
            "size_bytes": destination.stat().st_size,
            "loudness": loudness,
        }
    finally:
        temporary.unlink(missing_ok=True)


def valid_composition(item, role, signature):
    if not isinstance(item, dict) or item.get("role") != role:
        return False
    if item.get("role_fingerprint") != signature:
        return False
    try:
        source = project_path(item.get("source_path"))
        prepared = project_path(item.get("path"))
        if not source.is_file() or not prepared.is_file():
            return False
        if sha256_file(source) != item.get("source_sha256"):
            return False
        if sha256_file(prepared) != item.get("sha256"):
            return False
        probe_audio(source, MIN_AUDIO_DURATION)
        probe_audio(prepared, MIN_AUDIO_DURATION)
        return True
    except Exception:
        return False


def build_role_compositions(
    api_key, model, prompts, role_durations, role_rows, signature,
):
    previous = read_json(CHECKPOINT)
    if previous.get("source_fingerprint") != signature:
        previous = read_json(MANIFEST)

    cached = previous.get("role_compositions", {})
    if not isinstance(cached, dict):
        cached = {}

    compositions = {}

    for role in SEGMENT_ROLES:
        duration = role_durations.get(role, 0.0)
        if duration <= 0:
            continue

        role_signature = fingerprint({
            "source_fingerprint": signature,
            "role": role,
            "prompt": prompts[role],
            "duration": round(duration, 4),
            "narration": [
                {
                    "scene_id": row["scene_id"],
                    "sha256": row["audio_sha256"],
                    "duration": round(row["duration_seconds"], 4),
                }
                for row in role_rows[role]
            ],
        })

        old = cached.get(role)
        if valid_composition(old, role, role_signature):
            compositions[role] = old
            log("MUSIC RESUME: reused", role, "composition.")
            continue

        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        source = SOURCE_DIR / f"{role}_source.audio"
        prepared = SOURCE_DIR / f"{role}_composition.mp3"

        source_duration = request_music(
            api_key, model, prompts[role], source
        )
        prepared_info = prepare_composition(
            source, prepared, max(MIN_AUDIO_DURATION, duration)
        )

        item = {
            "role": role,
            "role_fingerprint": role_signature,
            "source_path": source.relative_to(ROOT).as_posix(),
            "source_sha256": sha256_file(source),
            "source_duration_seconds": round(source_duration, 4),
            "path": prepared.relative_to(ROOT).as_posix(),
            "sha256": prepared_info["sha256"],
            "duration_seconds": round(
                prepared_info["duration_seconds"], 4
            ),
            "required_role_duration_seconds": round(duration, 4),
            "loudness": prepared_info["loudness"],
            "independent_composition": True,
        }
        compositions[role] = item

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": signature,
            "completed_role_compositions": sorted(compositions),
            "role_compositions": compositions,
            "api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "updated_at": time.time(),
        })
        log("MUSIC COMPOSITION COMPLETE:", role)

    return compositions

def extract_segment(source, destination, start, duration):
    if duration <= 0.05:
        raise RuntimeError("Music segment duration is invalid.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    fade_in = min(0.20, duration / 8)
    fade_out = min(0.35, duration / 6)
    fade_start = max(0.0, duration - fade_out)

    try:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.4f}", "-i", str(source),
            "-t", f"{duration:.4f}", "-vn",
            "-af",
            (
                f"afade=t=in:d={fade_in:.4f},"
                f"afade=t=out:st={fade_start:.4f}:d={fade_out:.4f}"
            ),
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-ar", "44100", "-ac", "2", str(temporary),
        ], 300)

        actual = probe_audio(temporary, 0.05)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Music segment duration mismatch: {actual:.3f}s "
                f"vs {duration:.3f}s."
            )

        os.replace(temporary, destination)
        return {
            "path": destination.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(destination),
            "duration_seconds": round(actual, 4),
            "role_source_start_seconds": round(start, 4),
            "loudness": measure_loudness(destination),
        }
    finally:
        temporary.unlink(missing_ok=True)


def build_role_segments(compositions, timeline, signature):
    offsets = {role: 0.0 for role in SEGMENT_ROLES}
    segments = []
    grouped = {role: [] for role in SEGMENT_ROLES}

    for row in timeline:
        role = row["role"]
        if role not in compositions:
            raise RuntimeError(
                f"Missing independent music composition for role: {role}"
            )

        composition = compositions[role]
        source = project_path(composition["path"])
        offset = offsets[role]
        duration = row["duration_seconds"]

        if offset + duration > composition["duration_seconds"] + 0.30:
            raise RuntimeError(
                f"Independent {role} composition is too short for "
                f"{row['scene_id']}."
            )

        safe_name = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", row["scene_id"]
        ).strip("_")
        if not safe_name:
            raise RuntimeError("Invalid narration scene ID for music segment.")

        target = SEGMENT_DIR / f"{safe_name}.mp3"
        segment_signature = fingerprint({
            "source_fingerprint": signature,
            "scene_id": row["scene_id"],
            "narration_sha256": row["audio_sha256"],
            "composition_sha256": composition["sha256"],
            "start": round(offset, 4),
            "duration": round(duration, 4),
        })

        old = None
        previous = read_json(MANIFEST)
        if previous.get("source_fingerprint") == signature:
            old_segments = previous.get("role_segments", [])
            if isinstance(old_segments, list):
                old = next(
                    (
                        item for item in old_segments
                        if isinstance(item, dict)
                        and item.get("scene_id") == row["scene_id"]
                        and item.get("segment_fingerprint") == segment_signature
                    ),
                    None,
                )

        reused = False
        if isinstance(old, dict):
            try:
                old_path = project_path(old.get("path"))
                if (
                    old_path.is_file()
                    and sha256_file(old_path) == old.get("sha256")
                    and abs(
                        probe_audio(old_path, 0.05) - duration
                    ) <= DURATION_TOLERANCE
                ):
                    item = dict(old)
                    reused = True
                else:
                    item = extract_segment(source, target, offset, duration)
            except Exception:
                item = extract_segment(source, target, offset, duration)
        else:
            item = extract_segment(source, target, offset, duration)

        item.update({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "role": role,
            "scene": row["scene"],
            "narration_sha256": row["audio_sha256"],
            "start_seconds": row["start_seconds"],
            "role_source_start_seconds": round(offset, 4),
            "composition_sha256": composition["sha256"],
            "segment_fingerprint": segment_signature,
            "segment_type": "independent_role_composition_excerpt",
            "reused": reused,
        })

        segments.append(item)
        grouped[role].append(item)
        offsets[role] += duration

    summaries = {}
    for role, items in grouped.items():
        summaries[role] = {
            "count": len(items),
            "segments": items,
            "total_duration_seconds": round(
                sum(item["duration_seconds"] for item in items), 4
            ),
            "independent_composition": role in compositions,
            "composition_path": (
                compositions[role]["path"] if role in compositions else None
            ),
            "composition_sha256": (
                compositions[role]["sha256"] if role in compositions else None
            ),
        }

    return segments, summaries


def build_master_from_segments(segments, duration):
    if not segments:
        raise RuntimeError("Cannot build master music without role segments.")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required.")

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = AUDIO_DIR / "flow_music_master_segments.txt"
    temporary = FINAL_AUDIO.with_name(FINAL_AUDIO.stem + ".building.mp3")
    temporary.unlink(missing_ok=True)

    try:
        with concat_file.open("w", encoding="utf-8", newline="\n") as handle:
            for item in segments:
                path = project_path(item["path"])
                if not path.is_file():
                    raise RuntimeError(
                        f"Music segment is missing: {item['scene_id']}"
                    )
                resolved = str(path.resolve())
                if "\n" in resolved or "\r" in resolved:
                    raise RuntimeError("Invalid newline in music segment path.")
                handle.write(
                    "file '" + resolved.replace("'", "'\\''") + "'\n"
                )

        fade = min(MASTER_TAIL_SECONDS, duration)
        fade_start = max(0.0, duration - fade)

        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-af",
            (
                "aresample=44100,"
                f"loudnorm=I={MUSIC_TARGET_LUFS}:"
                f"TP={MUSIC_TRUE_PEAK_LIMIT}:LRA=10,"
                f"apad=pad_dur={MASTER_TAIL_SECONDS:.3f},"
                f"atrim=duration={duration:.3f},"
                f"afade=t=out:st={fade_start:.3f}:d={fade:.3f}"
            ),
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            "-map_metadata", "-1", str(temporary),
        ], 900)

        actual = probe_audio(temporary, MIN_AUDIO_DURATION)
        if abs(actual - duration) > DURATION_TOLERANCE:
            raise RuntimeError(
                f"Master duration mismatch: {actual:.3f}s vs {duration:.3f}s."
            )

        loudness = measure_loudness(temporary)
        validate_master_loudness(loudness)
        os.replace(temporary, FINAL_AUDIO)

        return {
            "duration_seconds": round(actual, 4),
            "sha256": sha256_file(FINAL_AUDIO),
            "size_bytes": FINAL_AUDIO.stat().st_size,
            "loudness": loudness,
        }
    finally:
        temporary.unlink(missing_ok=True)
        concat_file.unlink(missing_ok=True)


def verify_segment_records(segments, timeline, narration_by_id):
    expected_ids = {row["scene_id"] for row in timeline}
    actual_ids = [item.get("scene_id") for item in segments]

    if (
        len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != expected_ids
    ):
        raise RuntimeError(
            "Role segments do not cover every narration record."
        )

    timeline_by_id = {row["scene_id"]: row for row in timeline}

    for item in segments:
        scene_id = item["scene_id"]
        narration = narration_by_id.get(scene_id)
        timeline_row = timeline_by_id.get(scene_id)

        if narration is None or timeline_row is None:
            raise RuntimeError(f"Music segment has no narration: {scene_id}")

        if item.get("narration_sha256") != narration["audio_sha256"]:
            raise RuntimeError(f"Music narration hash mismatch: {scene_id}")
        if item.get("role") != narration["role"]:
            raise RuntimeError(f"Music role mismatch: {scene_id}")
        if int(item.get("part", 0)) != narration["part"]:
            raise RuntimeError(f"Music Part mismatch: {scene_id}")
        if abs(
            float(item.get("start_seconds", -999))
            - timeline_row["start_seconds"]
        ) > 0.05:
            raise RuntimeError(f"Music timeline mismatch: {scene_id}")

        path = project_path(item.get("path"))
        if not path.is_file() or sha256_file(path) != item.get("sha256"):
            raise RuntimeError(f"Music segment SHA256 mismatch: {scene_id}")

        actual = probe_audio(path, 0.05)
        if abs(actual - narration["duration_seconds"]) > DURATION_TOLERANCE:
            raise RuntimeError(f"Music is not narration-aligned: {scene_id}")


def verify_compositions(compositions):
    for role, item in compositions.items():
        if role not in SEGMENT_ROLES:
            raise RuntimeError(f"Unknown role composition: {role}")

        source = project_path(item.get("source_path"))
        prepared = project_path(item.get("path"))
        if not source.is_file() or not prepared.is_file():
            raise RuntimeError(f"Independent {role} composition is missing.")
        if sha256_file(source) != item.get("source_sha256"):
            raise RuntimeError(f"Independent {role} source hash mismatch.")
        if sha256_file(prepared) != item.get("sha256"):
            raise RuntimeError(f"Independent {role} composition hash mismatch.")

        probe_audio(source, MIN_AUDIO_DURATION)
        probe_audio(prepared, MIN_AUDIO_DURATION)


def verify_existing(signature, duration, timeline):
    previous = read_json(MANIFEST)
    if (
        previous.get("status") != "completed"
        or previous.get("version") != VERSION
        or previous.get("source_fingerprint") != signature
        or previous.get("provider") != "google_lyria"
        or previous.get("generation_method")
        != "four_independent_role_compositions"
    ):
        return False

    try:
        path = project_path(previous.get("path") or previous.get("audio_path"))
        if path != FINAL_AUDIO.resolve():
            return False

        actual = probe_audio(path, MIN_AUDIO_DURATION)
        if previous.get("sha256") != sha256_file(path):
            return False
        if abs(actual - duration) > DURATION_TOLERANCE:
            return False
        if abs(
            float(previous.get("required_duration_seconds")) - duration
        ) > 0.01:
            return False

        compositions = previous.get("role_compositions")
        segments = previous.get("role_segments")
        if not isinstance(compositions, dict) or not compositions:
            return False
        if not isinstance(segments, list):
            return False

        verify_compositions(compositions)
        narration_by_id = {row["scene_id"]: row for row in timeline}
        verify_segment_records(segments, timeline, narration_by_id)

        loudness = measure_loudness(path)
        validate_master_loudness(loudness)

        log(
            "RESUME: verified role compositions, master music "
            "and narration-aligned segments reused."
        )
        return True
    except Exception as exc:
        log("RESUME REJECTED:", str(exc)[:500])
        return False


def validate_published_manifest(signature, final_checksum, duration, timeline):
    published = read_json(MANIFEST)
    if (
        published.get("status") != "completed"
        or published.get("version") != VERSION
        or published.get("sha256") != final_checksum
        or published.get("source_fingerprint") != signature
        or published.get("path") != FINAL_AUDIO.relative_to(ROOT).as_posix()
        or published.get("generation_method")
        != "four_independent_role_compositions"
    ):
        raise RuntimeError("Published Flow Music manifest failed verification.")

    actual = probe_audio(FINAL_AUDIO, MIN_AUDIO_DURATION)
    if abs(actual - duration) > DURATION_TOLERANCE:
        raise RuntimeError("Published master duration mismatch.")

    compositions = published.get("role_compositions")
    segments = published.get("role_segments")
    if not isinstance(compositions, dict) or not compositions:
        raise RuntimeError("Published role compositions are missing.")
    if not isinstance(segments, list):
        raise RuntimeError("Published role segments are missing.")

    verify_compositions(compositions)
    narration_by_id = {row["scene_id"]: row for row in timeline}
    verify_segment_records(segments, timeline, narration_by_id)

    if len(segments) != len(timeline):
        raise RuntimeError("Published role segment count mismatch.")

    loudness = measure_loudness(FINAL_AUDIO)
    validate_master_loudness(loudness)
    return published


def write_unavailable(reason, model=None, duration=None, signature=None):
    record = {
        "status": "unavailable",
        "provider": "google_lyria",
        "product": "Google Lyria",
        "model": model or DEFAULT_MODEL,
        "reason": str(reason)[:2000],
        "fallback": "sound_design_local_fallback",
        "paid_services_used": API_CALLS > 0,
        "external_api_calls": API_CALLS,
        "version": VERSION,
    }
    if duration is not None:
        record["required_duration_seconds"] = round(float(duration), 3)
    if signature:
        record["source_fingerprint"] = signature

    write_json(MANIFEST, record)
    write_json(CHECKPOINT, {
        "status": "unavailable",
        "version": VERSION,
        "reason": str(reason)[:2000],
        "source_fingerprint": signature,
        "api_calls": API_CALLS,
        "paid_services_used": API_CALLS > 0,
        "updated_at": time.time(),
    })


def write_disabled():
    write_json(MANIFEST, {
        "status": "disabled",
        "provider": "google_lyria",
        "reason": "Background music is disabled in Input/topic.txt.",
        "paid_services_used": False,
        "external_api_calls": 0,
        "version": VERSION,
    })
    write_json(CHECKPOINT, {
        "status": "disabled",
        "version": VERSION,
    })


def main():
    global API_CALLS

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)

    config = read_settings()
    enabled = truthy(
        config.get("MUSIC_ENABLED")
        or config.get("MUSIC")
        or config.get("BACKGROUND_MUSIC", "true"),
        True,
    )

    if not enabled:
        write_disabled()
        log("MUSIC DISABLED.")
        return 0

    model = validate_model(config)
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

        target_duration = round(
            narration_duration + MASTER_TAIL_SECONDS, 3
        )
        timeline, timeline_duration = build_timeline(rows)

        if abs(timeline_duration - narration_duration) > 0.05:
            raise RuntimeError("Narration timeline duration mismatch.")

        role_rows = {
            role: [row for row in rows if row["role"] == role]
            for role in SEGMENT_ROLES
        }
        role_durations = {
            role: round(
                sum(row["duration_seconds"] for row in role_rows[role]),
                4,
            )
            for role in SEGMENT_ROLES
        }

        prompts = {
            role: build_role_prompt(
                title, story_text, summary, role, rows, config
            )
            for role in SEGMENT_ROLES
            if role_durations[role] > 0
        }

        source_sig = source_fingerprint(
            title,
            story_text,
            summary,
            rows,
            config,
            model,
            target_duration,
            prompts,
            role_durations,
        )

        if verify_existing(source_sig, target_duration, timeline):
            previous = read_json(MANIFEST)
            write_json(CHECKPOINT, {
                "status": "completed",
                "version": VERSION,
                "source_fingerprint": source_sig,
                "sha256": previous.get("sha256"),
                "reused": True,
                "api_calls": 0,
                "paid_services_used": False,
                "role_composition_count": len(
                    previous.get("role_compositions", {})
                ),
                "role_segment_count": len(
                    previous.get("role_segments", [])
                ),
                "updated_at": time.time(),
            })
            return 0

        if not billable_allowed(config):
            write_unavailable(
                "Billable music generation is disabled by configuration.",
                model=model,
                duration=target_duration,
                signature=source_sig,
            )
            log("LYRIA SKIPPED: local sound-design fallback remains available.")
            return 0

        api_key = get_api_key()
        if not api_key:
            write_unavailable(
                "GEMINI_API_KEY and GOOGLE_API_KEY are not configured.",
                model=model,
                duration=target_duration,
                signature=source_sig,
            )
            log("LYRIA UNAVAILABLE: local sound-design fallback remains available.")
            return 0

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "target_duration_seconds": target_duration,
            "narration_records": len(rows),
            "role_summary": role_summary,
            "role_durations": role_durations,
            "completed_role_compositions": [],
            "api_calls_started": 0,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("GOOGLE LYRIA — INDEPENDENT ROLE COMPOSITIONS")
        log("MODEL:", model)
        log("STORY:", title)
        log("PARTS:", len(role_summary["parts"]))
        log("ACTUAL SCENES PER PART:", role_summary["actual_scene_counts"])
        log("NARRATION RECORDS:", len(rows))
        log("ROLE DURATIONS:", role_durations)
        log("NARRATION DURATION:", round(narration_duration, 3))
        log("REQUIRED MUSIC DURATION:", target_duration)
        log("TARGET MUSIC LOUDNESS:", MUSIC_TARGET_LUFS, "LUFS")
        log("=" * 60)

        compositions = build_role_compositions(
            api_key,
            model,
            prompts,
            role_durations,
            role_rows,
            source_sig,
        )

        expected_roles = {
            role for role in SEGMENT_ROLES if role_durations[role] > 0
        }
        if set(compositions) != expected_roles:
            raise RuntimeError(
                "Independent role composition coverage mismatch."
            )

        verify_compositions(compositions)

        write_json(CHECKPOINT, {
            "status": "in_progress",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "target_duration_seconds": target_duration,
            "completed_role_compositions": sorted(compositions),
            "role_compositions": compositions,
            "api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "updated_at": time.time(),
        })

        role_segments, role_segment_summary = build_role_segments(
            compositions, timeline, source_sig
        )

        narration_by_id = {row["scene_id"]: row for row in rows}
        verify_segment_records(
            role_segments, timeline, narration_by_id
        )

        required_roles = {
            "hook": role_summary["hook_count"],
            "main": role_summary["main_count"],
            "suspense": role_summary["suspense_count"],
            "resolution": role_summary["resolution_count"],
        }
        actual_roles = {
            role: sum(1 for item in role_segments if item["role"] == role)
            for role in SEGMENT_ROLES
        }
        if actual_roles != required_roles:
            raise RuntimeError(
                f"Music role counts mismatch: expected={required_roles}, "
                f"found={actual_roles}."
            )

        master = build_master_from_segments(
            role_segments, target_duration
        )
        final_checksum = sha256_file(FINAL_AUDIO)
        source_checksum = fingerprint({
            role: item["source_sha256"]
            for role, item in compositions.items()
        })

        record = {
            "status": "completed",
            "provider": "google_lyria",
            "product": "Google Lyria",
            "model": model,
            "generation_method": "four_independent_role_compositions",
            "source_audio": {
                role: item["source_path"]
                for role, item in compositions.items()
            },
            "path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "audio_path": FINAL_AUDIO.relative_to(ROOT).as_posix(),
            "duration_seconds": round(master["duration_seconds"], 3),
            "actual_audio_duration_seconds": round(
                master["duration_seconds"], 3
            ),
            "required_duration_seconds": target_duration,
            "narration_duration_seconds": round(narration_duration, 3),
            "music_tail_seconds": MASTER_TAIL_SECONDS,
            "sha256": final_checksum,
            "source_sha256": source_checksum,
            "source_fingerprint": source_sig,
            "instrumental_only": True,
            "lyrics_requested": False,
            "narration_duration_source": (
                "sum_of_ffprobe_measured_tts_audio_files"
            ),
            "narration_records": len(rows),
            "role_summary": role_summary,
            "role_compositions": compositions,
            "role_composition_count": len(compositions),
            "role_composition_policy": (
                "Independent compositions for Hook, Main, Suspense and "
                "Resolution; each narration segment uses its matching role."
            ),
            "role_segments": role_segments,
            "role_segment_summary": role_segment_summary,
            "role_segment_directory": SEGMENT_DIR.relative_to(ROOT).as_posix(),
            "role_segment_policy": (
                "Every narration record receives a verified segment from "
                "its independently generated role composition."
            ),
            "role_segment_timeline": timeline,
            "actual_scene_counts": role_summary["actual_scene_counts"],
            "scenes_min": role_summary["scenes_min"],
            "scenes_max": role_summary["scenes_max"],
            "story_aware_prompt": True,
            "story_beginning_considered": True,
            "story_middle_considered": True,
            "story_ending_considered": True,
            "common_story_identity_across_independent_roles": True,
            "narration_headroom": {
                "target_integrated_lufs": MUSIC_TARGET_LUFS,
                "true_peak_limit_dbtp": MUSIC_TRUE_PEAK_LIMIT,
                "measured": master["loudness"],
                "speech_midrange_preserved": True,
                "final_ducking_applied_by": "render_video.py",
            },
            "master_loudness": master["loudness"],
            "segment_loudness": {
                item["scene_id"]: item["loudness"]
                for item in role_segments
            },
            "timeline": timeline,
            "external_api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "size_bytes": master["size_bytes"],
            "version": VERSION,
            "completed_at": time.time(),
        }

        write_json(MANIFEST, record)
        published = validate_published_manifest(
            source_sig,
            final_checksum,
            target_duration,
            timeline,
        )

        write_json(CHECKPOINT, {
            "status": "completed",
            "version": VERSION,
            "source_fingerprint": source_sig,
            "sha256": final_checksum,
            "duration_seconds": round(master["duration_seconds"], 3),
            "target_duration_seconds": target_duration,
            "role_composition_count": len(
                published["role_compositions"]
            ),
            "role_composition_sha256": {
                role: item["sha256"]
                for role, item in published["role_compositions"].items()
            },
            "role_segment_count": len(published["role_segments"]),
            "role_segment_sha256": {
                item["scene_id"]: item["sha256"]
                for item in published["role_segments"]
            },
            "api_calls": API_CALLS,
            "paid_services_used": API_CALLS > 0,
            "updated_at": time.time(),
        })

        log("=" * 60)
        log("LYRIA MUSIC READY.")
        log("MODEL:", model)
        log("INDEPENDENT COMPOSITIONS:", sorted(compositions))
        log("ROLE SEGMENTS:", len(role_segments))
        log("FINAL DURATION:", round(master["duration_seconds"], 2))
        log("INTEGRATED LOUDNESS:", master["loudness"]["integrated_lufs"])
        log("TRUE PEAK:", master["loudness"]["true_peak_dbtp"])
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