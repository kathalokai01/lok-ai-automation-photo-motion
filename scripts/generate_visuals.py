
#!/usr/bin/env python3
"""Prepare resumable photo-motion image jobs with scene-specific continuity."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format

INPUT_FILE = ROOT / "Input" / "topic.txt"
SCENES_FILE = ROOT / "output" / "scenes" / "scenes.json"
BIBLE_FILE = ROOT / "output" / "story" / "character_bible.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"
VISUAL_DIR = ROOT / "output" / "visuals"
JOBS_FILE = VISUAL_DIR / "visual_jobs.json"
MANIFEST_FILE = VISUAL_DIR / "image_manifest.json"

VERSION = "photo-motion-11.0"
MIN_IMAGE_BYTES = 5000

# Keep synchronized with free_image_generator.py.
QUALITY = (
    "Photorealistic live-action cinematic photography. Realistic Indian "
    "people, locations, clothing and architecture. Natural skin texture, "
    "believable anatomy, realistic eyes, natural lighting and shadows. "
    "Maintain recurring character identity, age, hairstyle and clothing. "
    "No cartoon, anime, illustration, CGI, text, logo or watermark."
)

NEGATIVE_PROMPT = (
    "cartoon, anime, illustration, painting, CGI, plastic skin, wax figure, "
    "distorted face, bad anatomy, extra limbs, duplicated people, blurry "
    "face, unrealistic eyes, text, subtitles, watermark, logo, collage, "
    "split screen, poster lettering, floating objects, inconsistent "
    "character identity, unrelated characters, wrong era, unrealistic "
    "clothing, oversaturated skin, impossible perspective"
)

DESCRIPTION_KEYS = (
    "visual_prompt", "image_prompt", "visual_description",
    "scene_description", "description", "prompt", "action", "text",
)
DURATION_KEYS = ("duration", "scene_duration", "duration_seconds", "seconds")

CATEGORY_KEYWORDS = {
    "horror": (
        "भूत", "प्रेत", "डरावनी", "खौफ", "चुड़ैल", "आत्मा",
        "हवेली", "शैतान", "horror", "ghost", "haunted", "witch",
        "demon", "scary", "supernatural",
    ),
    "mystery": (
        "रहस्य", "गुमशुदा", "गायब", "पहेली", "अनजान", "अज्ञात",
        "खोज", "mystery", "secret", "unknown", "disappearance",
        "investigation",
    ),
    "devotional": (
        "भक्ति", "भगवान", "देवी", "देवता", "मंदिर", "पूजा",
        "कृष्ण", "राम", "शिव", "विष्णु", "हनुमान", "दुर्गा",
        "गणेश", "पुराण", "अवतार", "divine", "devotional",
        "temple", "krishna", "rama", "shiva", "goddess", "mythology",
    ),
    "historical": (
        "राजा", "रानी", "सम्राट", "महाराज", "युद्ध", "किला",
        "इतिहास", "योद्धा", "साम्राज्य", "historical", "king",
        "queen", "emperor", "warrior", "battle", "kingdom",
    ),
    "emotional": (
        "माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी", "प्यार",
        "प्रेम", "बलिदान", "दर्द", "emotional", "family",
        "mother", "father", "love", "sacrifice", "ईमान", "गरीबी",
        "honesty", "poverty",
    ),
    "nature": (
        "जंगल", "वन", "पहाड़", "नदी", "प्रकृति", "गाँव", "गांव",
        "किसान", "forest", "nature", "mountain", "river",
        "village", "farmer", "wildlife",
    ),
}


def log(*values):
    print(*values, flush=True)


def clean(value, limit=2000):
    if isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def fingerprint(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def read_json(path, required=True):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def first_story_line(value):
    for line in str(value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[A-Z_][A-Z0-9_]*\s*=", line):
            continue
        return clean(line, 250)
    return ""


def saved_title():
    if not TITLE_FILE.is_file():
        return ""
    try:
        for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
            if clean(line):
                return clean(line, 200)
    except OSError as exc:
        log("WARNING: Saved title unavailable:", exc)
    return ""


def resolve_topic(config):
    topic = clean(config.get("TOPIC", ""), 250)
    if topic:
        return topic
    story_text = str(config.get("STORY_TEXT", "")).strip()
    if story_text:
        return first_story_line(story_text) or saved_title() or "Indian story"
    return saved_title() or "Indian cinematic story"


def resolve_title(topic, config):
    explicit = clean(config.get("TOPIC", ""), 250)
    if explicit:
        return explicit
    story_text = str(config.get("STORY_TEXT", "")).strip()
    if story_text:
        return first_story_line(story_text) or topic
    return saved_title() or topic


def detect_category(text):
    normalized = str(text or "").casefold()
    for category, words in CATEGORY_KEYWORDS.items():
        if any(word.casefold() in normalized for word in words):
            return category
    return "general"


def category_direction(category):
    directions = {
        "horror": (
            "Use believable low light, restrained shadows and suspenseful "
            "framing. Do not invent monsters or events."
        ),
        "mystery": (
            "Use grounded visual clues and suspense. Do not reveal or invent "
            "plot events absent from the scene."
        ),
        "devotional": (
            "Use respectful devotional imagery only when called for. Do not "
            "invent deities, miracles or religious symbols."
        ),
        "historical": (
            "Use period-appropriate architecture, fabrics, tools and materials. "
            "Avoid anachronistic objects."
        ),
        "emotional": "Emphasize believable expressions and natural body language.",
        "nature": "Use realistic landscapes, vegetation, weather and materials.",
        "general": "Match lighting, color, composition and setting to the actual scene.",
    }
    return directions.get(category, directions["general"])


def extract_scenes(data):
    if isinstance(data, list):
        scenes = data
    elif isinstance(data, dict):
        scenes = next(
            (
                data[key] for key in ("scenes", "scene_list", "items", "data")
                if isinstance(data.get(key), list)
            ),
            None,
        )
        if scenes is None:
            raise RuntimeError("scenes.json must contain a scenes list.")
    else:
        raise RuntimeError("scenes.json must be an object or list.")

    if not scenes:
        raise RuntimeError("scenes.json contains no scenes.")
    if not all(isinstance(item, dict) for item in scenes):
        raise RuntimeError("Every scene must be a JSON object.")
    return scenes


def get_text(item, keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def positive_number(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (TypeError, ValueError):
        pass
    return None


def scene_duration(scene, config):
    for key in DURATION_KEYS:
        value = positive_number(scene.get(key))
        if value is not None and 0.5 <= value <= 3600:
            return round(value, 3)
    value = positive_number(config.get("SCENE_DURATION"))
    if value is not None and 0.5 <= value <= 3600:
        return round(value, 3)
    return 5.0


def load_character_bible():
    data = read_json(BIBLE_FILE, required=False)
    if not isinstance(data, dict):
        log("NOTICE: Character Bible unavailable; continuing without it.")
        return {}
    return data


def scene_mapping(scene, index, seen):
    part = scene.get("part", 1)
    local = scene.get("scene", scene.get("local_scene", index))
    if isinstance(part, (dict, list)) or isinstance(local, (dict, list)):
        raise RuntimeError(f"Scene {index}: invalid part/scene identifiers.")
    try:
        part, local = int(part), int(local)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Scene {index}: part and scene must be integers."
        ) from exc
    if part < 1 or local < 1:
        raise RuntimeError(f"Scene {index}: identifiers must be positive.")
    pair = (part, local)
    if pair in seen:
        raise RuntimeError(f"Duplicate scene mapping: {pair}")
    seen.add(pair)
    return pair


def validate_order(mappings):
    previous = (0, 0)
    for index, current in enumerate(mappings, 1):
        if current <= previous:
            raise RuntimeError(
                f"Scene order is not part-then-scene at global scene {index}: "
                f"{current}. Fix scenes.json before generating images."
            )
        previous = current


def collect_scene_context(scene, bible):
    """Select only continuity details relevant to this scene."""
    scene_text = " ".join(
        clean(scene.get(key), 1200)
        for key in (
            "title", "narration", "dialogue", "visual_prompt",
            "image_prompt", "visual", "description", "action",
            "character_name", "location", "setting",
        )
    ).casefold()

    explicit_names = {
        clean(scene.get(key), 120).casefold()
        for key in ("character_name", "character", "characters")
        if isinstance(scene.get(key), str) and clean(scene.get(key))
    }

    characters = bible.get("characters", [])
    selected = []
    if isinstance(characters, list):
        for character in characters:
            if not isinstance(character, dict):
                continue
            name = clean(character.get("name"), 120)
            if not name:
                continue
            normalized_name = name.casefold()
            if (
                normalized_name in explicit_names
                or normalized_name in scene_text
            ):
                details = []
                for key in (
                    "role", "age", "gender", "appearance", "face_features",
                    "skin_tone", "hair", "eyes", "body_features", "clothing",
                    "distinctive_features", "visual_identity", "continuity_notes",
                ):
                    value = clean(character.get(key), 240)
                    if value and not value.casefold().startswith(
                        ("not specified", "not inferred", "do not invent")
                    ):
                        details.append(f"{key.replace('_', ' ')}: {value}")

                source_details = character.get("source_details", [])
                if isinstance(source_details, list):
                    for detail in source_details[:4]:
                        detail = clean(detail, 240)
                        if detail and detail not in details:
                            details.append(detail)

                selected.append({
                    "name": name,
                    "details": details[:8],
                })

    # Scene-level explicit visual details take priority over inferred context.
    scene_details = []
    for key, label in (
        ("character_name", "character"),
        ("age", "age"),
        ("appearance", "appearance"),
        ("clothing", "clothing"),
        ("location", "location"),
        ("setting", "setting"),
        ("time_period", "time period"),
        ("weather", "weather"),
        ("time_of_day", "time of day"),
    ):
        value = clean(scene.get(key), 300)
        if value:
            scene_details.append(f"{label}: {value}")

    world = bible.get("world", {})
    world_details = []
    if isinstance(world, dict):
        for key in ("setting", "time_period", "geography"):
            value = clean(world.get(key), 350)
            if value and not value.casefold().startswith("not explicitly specified"):
                world_details.append(f"{key.replace('_', ' ')}: {value}")

    # Keep the context bounded to avoid bloated prompts.
    selected = selected[:8]
    scene_details = scene_details[:8]
    world_details = world_details[:3]
    return selected, scene_details, world_details


def build_prompt(
    mode, dimensions, topic, title, category, part, local_scene,
    description, scene, bible,
):
    if mode == "short":
        framing = (
            "Vertical 9:16 mobile-video composition. Keep faces and action "
            "away from extreme edges, with clear foreground and background depth."
        )
    else:
        framing = (
            "Horizontal 16:9 cinematic composition with balanced foreground, "
            "middle-ground and background."
        )

    characters, scene_details, world_details = collect_scene_context(scene, bible)
    context = []

    if characters:
        for character in characters:
            detail_text = "; ".join(character["details"])
            line = f"- {character['name']}"
            if detail_text:
                line += f": {detail_text}"
            context.append(line)

    continuity = ""
    if context:
        continuity += (
            "\nRELEVANT CHARACTER CONTINUITY (only these scene-relevant entries):\n"
            + "\n".join(context)
        )
    if scene_details:
        continuity += "\nEXPLICIT SCENE DETAILS:\n- " + "\n- ".join(scene_details)
    if world_details:
        continuity += "\nWORLD CONTINUITY:\n- " + "\n- ".join(world_details)

    if continuity:
        continuity += (
            "\nUse these details only when consistent with the scene description. "
            "Explicit scene details override general bible notes. Unspecified "
            "physical traits must not be fabricated."
        )

    return (
        "Create one original photorealistic live-action cinematic photograph "
        "for an Indian story video.\n"
        f"STORY TOPIC: {topic}\n"
        f"STORY TITLE: {title}\n"
        f"GENRE: {category}\n"
        f"GENRE DIRECTION: {category_direction(category)}\n"
        f"FORMAT: {mode}; target dimensions: {dimensions}.\n"
        f"PART: {part}; LOCAL SCENE: {local_scene}.\n"
        f"SCENE TO DEPICT: {description}\n"
        "Depict the actual characters, action, location, time, weather and "
        "emotion. Use believable expressions, realistic lighting and culturally "
        "appropriate Indian details when relevant. Create one coherent photograph, "
        "not a collage, split screen, poster or slideshow. Do not invent unrelated "
        "people, objects, costumes, locations or plot events. No lettering, "
        f"captions, logos or watermarks. {framing}{continuity}"
    )


def image_prompt_hash(prompt, negative_prompt):
    # Must match free_image_generator.py make_prompt() exactly.
    combined = f"{prompt}\n\nQuality: {QUALITY}\n\nAvoid: {negative_prompt}"
    return sha256_text(combined)


def previous_manifest_rows():
    data = read_json(MANIFEST_FILE, required=False)
    rows = data.get("scenes", []) if isinstance(data, dict) else data
    result = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                number = int(row.get("global_scene", row.get("scene")))
            except (TypeError, ValueError):
                continue
            if number > 0:
                result[number] = row
    return result


def valid_existing_image(path, dimensions):
    try:
        from PIL import Image
        path = Path(path)
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return False
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size == dimensions and image.format in ("PNG", "JPEG", "WEBP")
    except Exception:
        return False


def main():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_FILE)
    topic = resolve_topic(config)
    title = resolve_title(topic, config)

    mode = str(get_format(config)).strip().lower()
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported video format: {mode}")

    dimensions = "1280x720" if mode == "full" else "720x1280"
    width, height = map(int, dimensions.split("x"))

    scenes = extract_scenes(read_json(SCENES_FILE))
    bible = load_character_bible()

    description_text = " ".join(
        get_text(scene, DESCRIPTION_KEYS) for scene in scenes
    )
    category = detect_category(f"{topic} {title} {description_text}")

    mappings = []
    seen_pairs = set()
    for index, scene in enumerate(scenes, 1):
        mappings.append(scene_mapping(scene, index, seen_pairs))
    validate_order(mappings)

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    previous = previous_manifest_rows()
    jobs = []
    manifest_rows = []

    for index, scene in enumerate(scenes, 1):
        part, local_scene = mappings[index - 1]
        description = get_text(scene, DESCRIPTION_KEYS)
        if not description:
            raise RuntimeError(
                f"Scene {index} has no visual prompt. "
                "Refusing to generate an unrelated image."
            )

        duration = scene_duration(scene, config)
        image_path = f"output/visuals/scene_{index:02d}.png"
        prompt = build_prompt(
            mode, dimensions, topic, title, category, part, local_scene,
            description, scene, bible,
        )
        prompt_hash = image_prompt_hash(prompt, NEGATIVE_PROMPT)
        scene_id = clean(
            scene.get("scene_id", f"part_{part:02d}_scene_{local_scene:02d}"),
            150,
        )

        job = {
            "scene": index,
            "global_scene": index,
            "global_scene_number": index,
            "scene_id": scene_id,
            "part": part,
            "local_scene": local_scene,
            "status": "image_required",
            "image_path": image_path,
            "prompt": prompt,
            "prompt_sha256": prompt_hash,
            "negative_prompt": NEGATIVE_PROMPT,
            "image_size": dimensions,
            "duration": duration,
            "motion_engine": "photo_motion",
            "output_video": f"output/photo_motion/scene_{index:04d}.mp4",
        }
        jobs.append(job)

        destination = ROOT / image_path
        old = previous.get(index, {})
        matching_prompt = old.get("prompt_sha256") == prompt_hash
        image_ok = valid_existing_image(destination, (width, height))
        old_status = str(old.get("status", ""))

        if (
            matching_prompt
            and image_ok
            and old_status not in {"failed", "image_required", "pending"}
        ):
            status = old_status
            if status not in {"generated", "existing", "reused", "completed"}:
                status = "existing"
            provider = old.get("provider") or "previous run"
        else:
            status = "image_required"
            provider = None

        manifest_rows.append({
            "scene": index,
            "global_scene": index,
            "global_scene_number": index,
            "scene_id": scene_id,
            "part": part,
            "local_scene": local_scene,
            "image": image_path,
            "image_path": image_path,
            "duration": duration,
            "status": status,
            "provider": provider,
            "prompt_sha256": prompt_hash,
        })

    if len(jobs) != len(manifest_rows):
        raise RuntimeError("Image jobs and manifest counts do not match.")

    ready = sum(
        row["status"] in {"generated", "existing", "reused", "completed"}
        for row in manifest_rows
    )

    source_fingerprint = fingerprint({
        "version": VERSION,
        "topic": topic,
        "title": title,
        "category": category,
        "format": mode,
        "dimensions": dimensions,
        "character_bible": bible,
        "scenes": [
            {
                "part": job["part"],
                "local_scene": job["local_scene"],
                "scene_id": job["scene_id"],
                "prompt_sha256": job["prompt_sha256"],
                "duration": job["duration"],
            }
            for job in jobs
        ],
    })

    metadata = {
        "version": VERSION,
        "source_fingerprint": source_fingerprint,
        "format": mode,
        "story_topic": topic,
        "story_title": title,
        "story_category": category,
        "image_size": dimensions,
        "total_scenes": len(jobs),
        "motion_engine": "photo_motion",
        "uses_wan2gp": False,
        "uses_colab": False,
    }

    write_json(JOBS_FILE, {**metadata, "jobs": jobs})
    write_json(MANIFEST_FILE, {
        **metadata,
        "images_ready": ready,
        "images_required": len(manifest_rows),
        "scenes": manifest_rows,
    })

    log("=" * 56)
    log("IMAGE JOB PREPARATION COMPLETED")
    log("Topic:", topic)
    log("Title:", title)
    log("Category:", category)
    log("Format:", mode)
    log("Dimensions:", dimensions)
    log("Scenes:", len(jobs))
    log("Reusable valid images:", ready)
    log("Images requiring generation:", len(jobs) - ready)
    log("Jobs:", JOBS_FILE.relative_to(ROOT))
    log("Manifest:", MANIFEST_FILE.relative_to(ROOT))
    log("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Image-job preparation cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"IMAGE JOB PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
