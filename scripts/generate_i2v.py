#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import boto3
from botocore.client import Config

from input_config import (
    load_input_config,
    normalize_format,
    get_max_retries,
)


# ============================================================
# PATHS
# ============================================================

CONFIG_FILE = Path("Input/topic.txt")
SCENES_FILE = Path("output/scenes/scenes.json")

VISUAL_DIR = Path("output/visuals")
AUDIO_DIR = Path("output/narration/audio")
I2V_DIR = Path("output/i2v")

JOBS_FILE = I2V_DIR / "i2v_jobs.json"


# ============================================================
# LOCAL CONFIG HELPERS
# ============================================================

def cfg_bool(config, key, default=False):
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in {"true", "yes", "on", "1"}:
        return True

    if text in {"false", "no", "off", "0"}:
        return False

    return bool(default)


# ============================================================
# LOAD INPUT
# ============================================================

CONFIG = load_input_config()

FORMAT = normalize_format(CONFIG)

MAX_RETRIES = get_max_retries(CONFIG)

RESUME_ENABLED = cfg_bool(
    CONFIG,
    "RESUME_ENABLED",
    True,
)

SKIP_COMPLETED_SCENES = cfg_bool(
    CONFIG,
    "SKIP_COMPLETED_SCENES",
    True,
)

SAVE_CHECKPOINT = cfg_bool(
    CONFIG,
    "SAVE_CHECKPOINT_AFTER_EACH_SCENE",
    True,
)


# ============================================================
# CLOUDFLARE AI
# ============================================================

ACCOUNT_ID = os.getenv(
    "CLOUDFLARE_ACCOUNT_ID",
    "",
).strip()

API_TOKEN = os.getenv(
    "CLOUDFLARE_API_TOKEN",
    "",
).strip()

MODEL = "alibaba/hh1.1-i2v"

API_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    "{account_id}/ai/run/{model}"
)


# ============================================================
# CLOUDFLARE R2
# ============================================================

R2_BUCKET = os.getenv(
    "CLOUDFLARE_R2_BUCKET",
    "",
).strip()

R2_ACCESS_KEY_ID = os.getenv(
    "CLOUDFLARE_R2_ACCESS_KEY_ID",
    "",
).strip()

R2_SECRET_ACCESS_KEY = os.getenv(
    "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    "",
).strip()

R2_PUBLIC_URL = os.getenv(
    "CLOUDFLARE_R2_PUBLIC_URL",
    "",
).strip().rstrip("/")


# ============================================================
# I2V SETTINGS
# ============================================================

INITIAL_BACKOFF = 20
MAX_BACKOFF = 300
REQUEST_TIMEOUT = 300


# ============================================================
# FORMAT RESOLUTION
# ============================================================

def get_i2v_resolution(format_name):

    if format_name == "short":
        return "720P"

    return "1080P"


I2V_RESOLUTION = get_i2v_resolution(
    FORMAT
)


# ============================================================
# VALIDATE CLOUDFLARE
# ============================================================

if not ACCOUNT_ID:
    raise RuntimeError(
        "CLOUDFLARE_ACCOUNT_ID is not set."
    )

if not API_TOKEN:
    raise RuntimeError(
        "CLOUDFLARE_API_TOKEN is not set."
    )


# ============================================================
# VALIDATE R2
# ============================================================

if not R2_BUCKET:
    raise RuntimeError(
        "CLOUDFLARE_R2_BUCKET is not set."
    )

if not R2_ACCESS_KEY_ID:
    raise RuntimeError(
        "CLOUDFLARE_R2_ACCESS_KEY_ID is not set."
    )

if not R2_SECRET_ACCESS_KEY:
    raise RuntimeError(
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY is not set."
    )

if not R2_PUBLIC_URL:
    raise RuntimeError(
        "CLOUDFLARE_R2_PUBLIC_URL is not set."
    )


# ============================================================
# R2 CLIENT
# ============================================================

R2_ENDPOINT = (
    f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
)


def create_r2_client():

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4"
        ),
    )


R2_CLIENT = create_r2_client()


# ============================================================
# DIRECTORIES
# ============================================================

I2V_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path, default):

    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return default


def save_json(path, data):

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


jobs = load_json(
    JOBS_FILE,
    {
        "status": "running",
        "format": FORMAT,
        "model": MODEL,
        "resolution": I2V_RESOLUTION,
        "jobs": []
    }
)


# ============================================================
# VISUAL FINDER
# ============================================================

def find_visual(part, scene):

    directory = (
        VISUAL_DIR /
        f"part_{part:02d}"
    )

    candidates = [
        directory / f"scene_{scene:02d}.png",
        directory / f"scene_{scene:02d}.jpg",
        directory / f"scene_{scene:02d}.jpeg",
        directory / f"scene_{scene}.png",
        directory / f"scene_{scene}.jpg",
        directory / f"scene_{scene}.jpeg",
    ]

    for path in candidates:

        if (
            path.exists()
            and path.stat().st_size > 1000
        ):
            return path

    return None


# ============================================================
# AUDIO FINDER
# ============================================================

def find_audio(part, scene):

    directory = (
        AUDIO_DIR /
        f"part_{part:02d}"
    )

    candidates = [
        directory / f"scene_{scene:02d}.mp3",
        directory / f"scene_{scene}.mp3",
        directory / f"scene_{scene:02d}.wav",
        directory / f"scene_{scene}.wav",
    ]

    for path in candidates:

        if (
            path.exists()
            and path.stat().st_size > 1000
        ):
            return path

    return None


# ============================================================
# AUDIO DURATION
# ============================================================

def get_audio_duration(path):

    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:

        raise RuntimeError(
            f"Unable to read audio duration: {path}"
        )

    try:

        return float(
            result.stdout.strip()
        )

    except Exception:

        raise RuntimeError(
            f"Invalid audio duration: {path}"
        )


# ============================================================
# I2V DURATION
# ============================================================

def choose_duration(audio_duration):

    target = audio_duration + 0.5

    target = max(
        3.0,
        min(
            15.0,
            target
        )
    )

    duration = int(
        round(target)
    )

    duration = max(
        3,
        min(
            15,
            duration
        )
    )

    return duration


# ============================================================
# R2 CONTENT TYPE
# ============================================================

def get_content_type(path):

    suffix = path.suffix.lower()

    if suffix == ".png":
        return "image/png"

    if suffix in {
        ".jpg",
        ".jpeg",
    }:
        return "image/jpeg"

    raise RuntimeError(
        f"Unsupported visual format: {path}"
    )


# ============================================================
# R2 OBJECT KEY
# ============================================================

def make_r2_key(
    visual_path,
    part,
    scene
):

    data = visual_path.read_bytes()

    digest = hashlib.sha256(
        data
    ).hexdigest()[:16]

    extension = (
        visual_path.suffix.lower()
    )

    if extension == ".jpeg":
        extension = ".jpg"

    return (
        f"i2v-inputs/"
        f"{FORMAT}/"
        f"part_{part:02d}/"
        f"scene_{scene:02d}_"
        f"{digest}"
        f"{extension}"
    )


# ============================================================
# UPLOAD VISUAL TO R2
# ============================================================

def upload_visual_to_r2(
    visual_path,
    part,
    scene
):

    key = make_r2_key(
        visual_path,
        part,
        scene
    )

    content_type = get_content_type(
        visual_path
    )

    print()
    print(
        "Uploading visual to R2:"
    )

    print(
        f"  File : {visual_path}"
    )

    print(
        f"  Key  : {key}"
    )

    try:

        with visual_path.open(
            "rb"
        ) as file:

            R2_CLIENT.upload_fileobj(
                file,
                R2_BUCKET,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl":
                        "public, max-age=31536000, immutable",
                },
            )

    except Exception as exc:

        raise RuntimeError(
            f"R2 upload failed for "
            f"{visual_path}: {exc}"
        )

    public_url = (
        f"{R2_PUBLIC_URL}/{key}"
    )

    print(
        "  R2 upload successful."
    )

    print(
        f"  Public URL: {public_url}"
    )

    return {
        "key": key,
        "url": public_url,
        "content_type": content_type,
    }


# ============================================================
# MOTION PROMPT
# ============================================================

def build_prompt(
    scene,
    part,
    scene_number,
    duration
):

    base = str(
        scene.get(
            "visual_prompt",
            scene.get(
                "prompt",
                ""
            )
        )
    ).strip()

    scene_description = str(
        scene.get(
            "description",
            ""
        )
    ).strip()

    action = str(
        scene.get(
            "action",
            ""
        )
    ).strip()

    if FORMAT == "short":

        format_instruction = """
This is an ORIGINAL short-form video scene.

Motion must begin immediately in the first moment.

Do not make the opening feel like a still photograph.

Create immediate visual curiosity and tension.

Use purposeful human movement,
environmental movement,
camera movement,
or a combination.

The scene must feel designed specifically
for a short video.

It must NOT feel like a clipped section
from a longer movie.

Maintain momentum throughout the shot.

Avoid a static opening.

Avoid a static ending.
"""

    else:

        format_instruction = """
This is a cinematic long-form video scene.

Create continuous natural motion throughout
the entire shot.

Use subtle human movement,
environmental movement,
and purposeful cinematic camera movement.

Do not make the scene feel like a photograph.
"""

    return f"""
Create a REALISTIC AI IMAGE-TO-VIDEO shot.

PART: {part}
SCENE: {scene_number}
TARGET DURATION: {duration} seconds

SOURCE IMAGE IS THE VISUAL REFERENCE.

{format_instruction}

SOURCE SCENE DESCRIPTION:
{scene_description}

SOURCE ACTION:
{action}

SOURCE VISUAL PROMPT:
{base}

ANIMATION REQUIREMENTS:

- Real human beings.
- Real-world environment.
- Photorealistic appearance.
- Natural human anatomy.
- Natural skin texture.
- Natural facial movement.
- Natural eye movement.
- Natural blinking when appropriate.
- Natural breathing.
- Realistic body movement.
- Realistic clothing movement.
- Realistic environmental motion.
- Physically believable lighting.
- Realistic shadows.
- Natural depth of field.
- Cinematic but believable camera movement.
- Preserve identity of every person.
- Preserve age.
- Preserve face.
- Preserve hairstyle.
- Preserve clothing.
- Preserve body proportions.
- Preserve location.
- Preserve important objects.
- Preserve continuity with the source image.
- Do not change characters into different people.
- Do not change the scene into another location.

CAMERA:

Use subtle real camera movement such as:

slow handheld movement,
controlled dolly movement,
slow push-in,
gentle tracking,
or realistic documentary-style movement.

The camera must not move randomly.

IMPORTANT:

The generated result must look like footage
captured with a real camera in the real world.

It must NOT look like:

a comic,
a cartoon,
an illustration,
anime,
3D animation,
CGI,
game graphics,
a slideshow,
a moving photograph,
or a painted image.

Do not freeze the subjects.

Do not simply zoom a still image.

Create genuine temporal motion between frames.

NEGATIVE PROMPT:

cartoon, comic, anime, manga, illustration,
painting, drawing, sketch, 3d render, CGI,
game graphics, plastic skin, doll face,
unrealistic anatomy, distorted face,
extra fingers, extra limbs, duplicate person,
face deformation, identity change,
age change, clothing change, location change,
object morphing, frozen pose, static image,
slideshow, moving photograph,
artificial camera motion, warping,
flickering, jitter, frame interpolation artifacts,
ghosting, duplicated body parts,
neon colors, fantasy environment,
surreal environment, low detail,
blurry face, deformed hands,
watermark, text, logo
""".strip()


# ============================================================
# CLOUDFLARE HTTP REQUEST
# ============================================================

def call_cloudflare(payload):

    url = API_URL.format(
        account_id=ACCOUNT_ID,
        model=MODEL
    )

    body = json.dumps(
        payload
    ).encode("utf-8")

    request = Request(
        url,
        data=body,
        headers={
            "Authorization":
                f"Bearer {API_TOKEN}",
            "Content-Type":
                "application/json",
        },
        method="POST",
    )

    try:

        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT
        ) as response:

            raw = response.read()

            return json.loads(
                raw.decode("utf-8")
            )

    except HTTPError as error:

        response_body = ""

        try:

            response_body = (
                error.read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

        except Exception:
            pass

        raise RuntimeError(
            f"HTTP {error.code}: "
            f"{response_body[:2000]}"
        )

    except URLError as error:

        raise RuntimeError(
            f"Network error: {error}"
        )


# ============================================================
# VIDEO RESPONSE
# ============================================================

def extract_video_bytes(result):

    if not isinstance(
        result,
        dict
    ):

        raise RuntimeError(
            "Invalid Cloudflare response."
        )

    if result.get(
        "success"
    ) is False:

        errors = result.get(
            "errors",
            []
        )

        raise RuntimeError(
            f"Cloudflare AI error: {errors}"
        )

    result_data = result.get(
        "result"
    )

    if isinstance(
        result_data,
        dict
    ):

        video = result_data.get(
            "video"
        )

        if isinstance(
            video,
            str
        ):

            if (
                video.startswith("http://")
                or video.startswith("https://")
            ):

                request = Request(video)

                with urlopen(
                    request,
                    timeout=REQUEST_TIMEOUT
                ) as response:

                    return response.read()

            try:

                return base64.b64decode(
                    video
                )

            except Exception:
                pass

        if isinstance(
            video,
            dict
        ):

            data = video.get(
                "data"
            )

            if data:

                return base64.b64decode(
                    data
                )

    raise RuntimeError(
        "Cloudflare response does not contain "
        "a usable video result."
    )


# ============================================================
# GENERATE ONE CLIP
# ============================================================

def generate_clip(
    part,
    scene_number,
    scene,
    visual_path,
    audio_path,
    output_path
):

    audio_duration = get_audio_duration(
        audio_path
    )

    duration = choose_duration(
        audio_duration
    )

    prompt = build_prompt(
        scene=scene,
        part=part,
        scene_number=scene_number,
        duration=duration
    )

    print()
    print("=" * 70)
    print(
        f"GENERATING I2V "
        f"Part {part} Scene {scene_number}"
    )
    print("=" * 70)

    print(
        f"Audio duration : {audio_duration:.2f}s"
    )

    print(
        f"I2V duration   : {duration}s"
    )

    print(
        f"I2V resolution : {I2V_RESOLUTION}"
    )

    print(
        f"Model          : {MODEL}"
    )

    r2_info = upload_visual_to_r2(
        visual_path=visual_path,
        part=part,
        scene=scene_number
    )

    image_url = r2_info["url"]

    payload = {
        "input": {
            "image": image_url,
            "prompt": prompt,
            "negative_prompt": (
                "cartoon, comic, anime, illustration, "
                "painting, CGI, 3D render, slideshow, "
                "static image, distorted face, "
                "identity change, body deformation, "
                "warping, flicker, jitter"
            ),
            "resolution": I2V_RESOLUTION,
            "duration": duration,
            "watermark": False,
        }
    }

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            print(
                f"Attempt {attempt}/{MAX_RETRIES}"
            )

            result = call_cloudflare(
                payload
            )

            video_bytes = extract_video_bytes(
                result
            )

            if not video_bytes:
                raise RuntimeError(
                    "Cloudflare returned empty video."
                )

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            output_path.write_bytes(
                video_bytes
            )

            if (
                output_path.stat().st_size < 1000
            ):

                output_path.unlink(
                    missing_ok=True
                )

                raise RuntimeError(
                    "Generated video is too small."
                )

            print(
                f"SUCCESS: {output_path}"
            )

            return {
                "duration": duration,
                "r2_key": r2_info["key"],
                "r2_url": r2_info["url"],
            }

        except Exception as exc:

            last_error = exc

            print(
                f"Attempt {attempt} failed: {exc}"
            )

            if attempt >= MAX_RETRIES:
                break

            delay = min(
                MAX_BACKOFF,
                INITIAL_BACKOFF
                * (2 ** (attempt - 1))
            )

            delay += random.randint(
                0,
                10
            )

            print(
                f"Waiting {delay}s before retry..."
            )

            time.sleep(
                delay
            )

    raise RuntimeError(
        f"Part {part} Scene {scene_number} "
        f"failed after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("       REAL AI IMAGE-TO-VIDEO GENERATION")
    print("=" * 70)

    print(
        f"Format              : {FORMAT}"
    )

    print(
        f"Resolution          : {I2V_RESOLUTION}"
    )

    print(
        f"Model               : {MODEL}"
    )

    print(
        f"Max retries         : {MAX_RETRIES}"
    )

    print(
        f"Resume enabled      : {RESUME_ENABLED}"
    )

    print(
        f"Skip completed      : {SKIP_COMPLETED_SCENES}"
    )

    print(
        f"R2 bucket           : {R2_BUCKET}"
    )

    print(
        f"R2 public URL       : {R2_PUBLIC_URL}"
    )

    if not SCENES_FILE.exists():

        raise RuntimeError(
            "output/scenes/scenes.json not found."
        )

    scenes_data = json.loads(
        SCENES_FILE.read_text(
            encoding="utf-8"
        )
    )

    if scenes_data.get(
        "status"
    ) != "completed":

        raise RuntimeError(
            "Scenes are not marked completed."
        )

    scenes = scenes_data.get(
        "scenes",
        []
    )

    if not scenes:

        raise RuntimeError(
            "No scenes found."
        )

    print(
        f"Scenes: {len(scenes)}"
    )

    old_jobs = {}

    for job in jobs.get(
        "jobs",
        []
    ):

        try:

            key = (
                int(job["part"]),
                int(job["scene"])
            )

            old_jobs[key] = job

        except Exception:
            continue

    final_jobs = []

    for scene in scenes:

        part = int(
            scene.get(
                "part",
                0
            )
        )

        scene_number = int(
            scene.get(
                "scene",
                0
            )
        )

        if part <= 0:
            raise RuntimeError(
                f"Invalid part: {part}"
            )

        if scene_number <= 0:
            raise RuntimeError(
                f"Invalid scene: {scene_number}"
            )

        visual_path = find_visual(
            part,
            scene_number
        )

        if visual_path is None:

            raise RuntimeError(
                f"Missing visual for "
                f"Part {part} "
                f"Scene {scene_number}"
            )

        audio_path = find_audio(
            part,
            scene_number
        )

        if audio_path is None:

            raise RuntimeError(
                f"Missing narration for "
                f"Part {part} "
                f"Scene {scene_number}"
            )

        output_path = (
            I2V_DIR
            / f"part_{part:02d}"
            / f"scene_{scene_number:02d}.mp4"
        )

        key = (
            part,
            scene_number
        )

        if (
            RESUME_ENABLED
            and SKIP_COMPLETED_SCENES
            and output_path.exists()
            and output_path.stat().st_size > 1000
        ):

            print()
            print(
                f"SKIP existing I2V: "
                f"Part {part} "
                f"Scene {scene_number}"
            )

            audio_duration = 0.0

            try:

                audio_duration = get_audio_duration(
                    audio_path
                )

            except Exception:
                pass

            old_job = old_jobs.get(
                key,
                {}
            )

            final_jobs.append(
                {
                    "part": part,
                    "scene": scene_number,
                    "status": "completed",
                    "model": MODEL,
                    "format": FORMAT,
                    "resolution": I2V_RESOLUTION,
                    "visual": str(visual_path),
                    "audio": str(audio_path),
                    "output": str(output_path),
                    "audio_duration": audio_duration,
                    "r2_key": old_job.get("r2_key"),
                    "r2_url": old_job.get("r2_url"),
                }
            )

            continue

        result = generate_clip(
            part=part,
            scene_number=scene_number,
            scene=scene,
            visual_path=visual_path,
            audio_path=audio_path,
            output_path=output_path,
        )

        audio_duration = get_audio_duration(
            audio_path
        )

        final_jobs.append(
            {
                "part": part,
                "scene": scene_number,
                "status": "completed",
                "model": MODEL,
                "format": FORMAT,
                "resolution": I2V_RESOLUTION,
                "visual": str(visual_path),
                "audio": str(audio_path),
                "output": str(output_path),
                "audio_duration": audio_duration,
                "requested_duration": result["duration"],
                "r2_key": result["r2_key"],
                "r2_url": result["r2_url"],
            }
        )

        if SAVE_CHECKPOINT:

            jobs["jobs"] = final_jobs
            jobs["status"] = "running"
            jobs["format"] = FORMAT
            jobs["model"] = MODEL
            jobs["resolution"] = I2V_RESOLUTION
            jobs["total"] = len(scenes)

            save_json(
                JOBS_FILE,
                jobs
            )

            print(
                f"Checkpoint saved after "
                f"Part {part} Scene {scene_number}"
            )

    expected = len(scenes)

    completed = 0

    for job in final_jobs:

        output = Path(
            job["output"]
        )

        if (
            output.exists()
            and output.stat().st_size > 1000
        ):
            completed += 1

    print()
    print("=" * 70)
    print("             I2V VALIDATION")
    print("=" * 70)

    print(
        f"Format     : {FORMAT}"
    )

    print(
        f"Resolution : {I2V_RESOLUTION}"
    )

    print(
        f"Completed  : {completed}/{expected}"
    )

    if completed != expected:

        raise RuntimeError(
            "I2V clip count mismatch."
        )

    jobs["jobs"] = final_jobs
    jobs["status"] = "completed"
    jobs["format"] = FORMAT
    jobs["model"] = MODEL
    jobs["resolution"] = I2V_RESOLUTION
    jobs["total"] = expected

    save_json(
        JOBS_FILE,
        jobs
    )

    print()
    print(
        "REAL AI IMAGE-TO-VIDEO GENERATION COMPLETE."
    )

    print(
        f"Output directory: {I2V_DIR}"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "Interrupted."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print(
            f"ERROR: {exc}"
        )

        sys.exit(1)
