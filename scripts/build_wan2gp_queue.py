#!/usr/bin/env python3

import copy
import json
import tempfile
import zipfile
from pathlib import Path

from PIL import Image


# ============================================================
# WAN2GP TEST 7 — FINAL PIPELINE QUEUE BUILDER
# ============================================================

BASE = Path(__file__).resolve().parents[1]

SCENES_FILE = BASE / "output" / "scenes" / "scenes.json"
VISUALS_DIR = BASE / "output" / "visuals"

OUTPUT_DIR = BASE / "output" / "i2v"
OUTPUT_ZIP = OUTPUT_DIR / "wan2gp_queue.zip"

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


# ============================================================
# TEST 7 — LOCKED STABLE SETTINGS
# ============================================================

WAN_MODEL_TYPE = "ti2v_2_2"

WAN_RESOLUTION = "480x832"
WAN_FRAMES = 33
WAN_FPS = "16"

WAN_STEPS = 16
WAN_SEED = 20261011

WAN_GUIDANCE = 2.0
WAN_FLOW_SHIFT = 5
WAN_MOTION_AMPLITUDE = 0.55

WAN_IMAGE_PROMPT_TYPE = "S"
WAN_VIDEO_PROMPT_TYPE = "S"

WAN_PROMPT_MODE = "FG"


# ============================================================
# TEST 7 REAL-LIVE-ACTION PROMPT
# ============================================================

FACE_MOTION_PROMPT = """
Photorealistic live-action documentary video.

Preserve the exact identity of the person from the reference image.
Preserve facial proportions, skin texture, hairstyle, clothing and environment.

The camera is locked and stable.

Natural subtle breathing.
Very small natural body and weight movement.
Very small natural posture adjustment.
Minimal natural head movement.
Head and neck remain stable.

FACE MICRO-MOTION:
Natural eye focus changes.
One subtle irregular natural blink.
Very tiny eyelid movement.
Eyes remain anatomically stable.

Extremely subtle natural lip and jaw micro-movement
as if the person is about to speak,
but the person does NOT visibly speak.

No exaggerated mouth opening.
No visible teeth.
No tongue.
Only tiny natural lip compression and release.
Very small natural jaw relaxation.

Keep facial identity identical from first frame to last frame.

Clothing has subtle natural movement.
Background has subtle realistic environmental movement.
Distant people may move naturally.
Environment and buildings remain stable.

Real-world live-action footage.
Natural human movement.
Photorealistic skin.
Realistic lighting.
Stable temporal consistency.
Real documentary camera.
"""


# ============================================================
# STRONG NEGATIVE PROMPT
# ============================================================

FACE_NEGATIVE_PROMPT = """
cartoon,
anime,
illustration,
painting,
CGI,
3D render,
artificial face,
plastic skin,

face morphing,
identity change,
different person,
facial warping,
facial distortion,
face deformation,
face ghosting,
double face,
duplicate face,

unstable eyes,
crossed eyes,
asymmetric eyes,
eye duplication,
extra eyelids,
missing eyelids,
exaggerated eye movement,
exaggerated blinking,

large mouth movement,
talking,
speaking,
lip sync,
open mouth,
teeth,
tongue,
mouth deformation,
lip deformation,
jaw deformation,
jaw stretching,

neck bending,
neck deformation,
head shaking,
head morphing,

blurred face,
smeared face,
flickering face,
temporal flicker,

background morphing,
moving buildings,
distorted buildings,
duplicated people,
distorted people,

excessive body motion,

camera movement,
camera shake,
camera rotation,
zoom,
pan,

slideshow,
photo animation,
photo montage,
static photograph,
frozen frame,

AI-generated look,
unnatural motion,
temporal inconsistency,
ghosting,
flicker,
glitch,
watermark,
logo,
text
"""


# ============================================================
# HELPERS
# ============================================================

def fail(message):
    print()
    print("ERROR:", message)
    raise SystemExit(1)


def load_json(path, description):
    if not path.exists():
        fail(f"{description} not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Could not parse {description}: {exc}")


def load_scenes():

    data = load_json(
        SCENES_FILE,
        "scenes.json"
    )

    if isinstance(data, dict):
        scenes = data.get("scenes")

    elif isinstance(data, list):
        scenes = data

    else:
        scenes = None

    if not isinstance(scenes, list):
        fail(
            "scenes.json does not contain a valid scenes array."
        )

    if not scenes:
        fail(
            "scenes.json contains zero scenes."
        )

    return sorted(
        scenes,
        key=lambda x: (
            int(x.get("part", 0)),
            int(x.get("scene", 0))
        )
    )


def find_visual(part, scene):

    part_dir = VISUALS_DIR / f"part_{part:02d}"

    for ext in IMAGE_EXTENSIONS:

        candidate = (
            part_dir /
            f"scene_{scene:02d}{ext}"
        )

        if candidate.exists():
            return candidate

    if part_dir.exists():

        matches = sorted(
            p
            for p in part_dir.glob(
                f"scene_{scene:02d}.*"
            )
            if p.suffix.lower()
            in IMAGE_EXTENSIONS
        )

        if matches:
            return matches[0]

    return None


def validate_image(path):

    try:

        with Image.open(path) as img:
            img.verify()

        with Image.open(path) as img:

            image = img.convert("RGB")

            width, height = image.size

        if width < 64 or height < 64:
            raise ValueError(
                f"image too small: {width}x{height}"
            )

        return width, height

    except Exception as exc:

        fail(
            f"Invalid visual image '{path}': {exc}"
        )


# ============================================================
# SCENE PROMPT
# ============================================================

def build_prompt(scene):

    visual = str(
        scene.get("visual_prompt") or ""
    ).strip()

    camera = str(
        scene.get("camera_prompt") or ""
    ).strip()

    lighting = str(
        scene.get("lighting_prompt") or ""
    ).strip()

    if not visual:

        fail(
            f"Part {scene.get('part')} "
            f"Scene {scene.get('scene')} "
            "has no visual_prompt."
        )

    sections = [

        visual,

        FACE_MOTION_PROMPT.strip(),

    ]

    if camera:

        sections.append(
            "Scene camera direction: "
            + camera
        )

    if lighting:

        sections.append(
            "Scene lighting direction: "
            + lighting
        )

    return "\n\n".join(sections)


# ============================================================
# NEGATIVE PROMPT
# ============================================================

def build_negative_prompt(scene):

    original = str(
        scene.get("negative_prompt") or ""
    ).strip()

    parts = []

    if original:
        parts.append(original)

    parts.append(
        FACE_NEGATIVE_PROMPT.strip()
    )

    result = []

    seen = set()

    for block in parts:

        for item in block.split(","):

            value = item.strip()

            key = value.lower()

            if value and key not in seen:

                seen.add(key)

                result.append(value)

    return ", ".join(result)


# ============================================================
# BUILD NATIVE WAN2GP TASK
# ============================================================

def make_task(
    scene,
    embedded_image_name
):

    try:

        part = int(scene["part"])
        scene_no = int(scene["scene"])

    except Exception:

        fail(
            "Scene has invalid part/scene values."
        )

    params = {

        # ----------------------------
        # MODEL
        # ----------------------------

        "model_type":
            WAN_MODEL_TYPE,

        # ----------------------------
        # IMAGE
        # ----------------------------

        "image_start":
            embedded_image_name,

        "image_prompt_type":
            WAN_IMAGE_PROMPT_TYPE,

        "video_prompt_type":
            WAN_VIDEO_PROMPT_TYPE,

        # ----------------------------
        # VIDEO
        # ----------------------------

        "resolution":
            WAN_RESOLUTION,

        "video_length":
            WAN_FRAMES,

        # IMPORTANT:
        # Wan2GP expects string here.
        "force_fps":
            WAN_FPS,

        # ----------------------------
        # QUALITY
        # ----------------------------

        "num_inference_steps":
            WAN_STEPS,

        "seed":
            WAN_SEED + (part * 100) + scene_no,

        "guidance_scale":
            WAN_GUIDANCE,

        "flow_shift":
            WAN_FLOW_SHIFT,

        "motion_amplitude":
            WAN_MOTION_AMPLITUDE,

        # ----------------------------
        # GENERATION
        # ----------------------------

        "repeat_generation":
            1,

        "batch_size":
            1,

        "multi_images_gen_type":
            0,

        "multi_prompts_gen_type":
            WAN_PROMPT_MODE,

        # ----------------------------
        # PROMPTS
        # ----------------------------

        "prompt":
            build_prompt(scene),

        "negative_prompt":
            build_negative_prompt(scene),

        # ----------------------------
        # OUTPUT
        # ----------------------------

        "output_filename":
            f"part_{part:02d}_scene_{scene_no:02d}.mp4"
    }

    return {

        "id":
            f"part_{part:02d}_scene_{scene_no:02d}",

        "params":
            params,

        "plugin_data":
            {}
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 64)
    print("WAN2GP TEST 7 — FINAL QUEUE BUILDER")
    print("=" * 64)

    print()
    print("Scenes :", SCENES_FILE)
    print("Visuals:", VISUALS_DIR)
    print("Output :", OUTPUT_ZIP)

    print()
    print("MODEL")
    print("model_type        :", WAN_MODEL_TYPE)
    print("resolution        :", WAN_RESOLUTION)
    print("frames            :", WAN_FRAMES)
    print("fps               :", WAN_FPS)
    print("steps             :", WAN_STEPS)
    print("guidance          :", WAN_GUIDANCE)
    print("flow_shift        :", WAN_FLOW_SHIFT)
    print("motion_amplitude  :", WAN_MOTION_AMPLITUDE)
    print("prompt_mode       :", WAN_PROMPT_MODE)

    scenes = load_scenes()

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if OUTPUT_ZIP.exists():
        OUTPUT_ZIP.unlink()

    tasks = []

    missing = []

    with tempfile.TemporaryDirectory(
        prefix="wan2gp_test7_"
    ) as temp_dir:

        temp_root = Path(temp_dir)

        # ----------------------------------------
        # BUILD EVERY SCENE
        # ----------------------------------------

        for index, scene in enumerate(
            scenes,
            start=1
        ):

            try:

                part = int(
                    scene["part"]
                )

                scene_no = int(
                    scene["scene"]
                )

            except Exception:

                fail(
                    f"Scene #{index} has invalid "
                    "part/scene values."
                )

            visual = find_visual(
                part,
                scene_no
            )

            if visual is None:

                missing.append(
                    f"part_{part:02d}/"
                    f"scene_{scene_no:02d}"
                )

                continue

            width, height = validate_image(
                visual
            )

            embedded_name = (
                f"task{index:03d}_"
                "image_start_0.png"
            )

            embedded_path = (
                temp_root /
                embedded_name
            )

            try:

                with Image.open(
                    visual
                ) as img:

                    img.convert(
                        "RGB"
                    ).save(
                        embedded_path,
                        format="PNG",
                        optimize=True
                    )

            except Exception as exc:

                fail(
                    f"Could not embed "
                    f"{visual}: {exc}"
                )

            task = make_task(
                scene,
                embedded_name
            )

            tasks.append(task)

            print(
                f"[{len(tasks):03d}] "
                f"Part {part:02d} "
                f"Scene {scene_no:02d} "
                f"| {width}x{height}"
            )

        # ----------------------------------------
        # MISSING IMAGES
        # ----------------------------------------

        if missing:

            print()
            print(
                "MISSING VISUALS:"
            )

            for item in missing:
                print(
                    " -",
                    item
                )

            fail(
                f"{len(missing)} scene visual(s) "
                "missing. Queue NOT created."
            )

        if not tasks:

            fail(
                "No valid scene tasks created."
            )

        # ----------------------------------------
        # QUEUE JSON
        # ----------------------------------------

        queue_json = (
            temp_root /
            "queue.json"
        )

        queue_json.write_text(
            json.dumps(
                tasks,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        # ----------------------------------------
        # BUILD ZIP
        # ----------------------------------------

        with zipfile.ZipFile(
            OUTPUT_ZIP,
            "w",
            compression=zipfile.ZIP_DEFLATED
        ) as zf:

            zf.write(
                queue_json,
                "queue.json"
            )

            for image_file in sorted(
                temp_root.glob(
                    "task*_image_start_0.png"
                )
            ):

                zf.write(
                    image_file,
                    image_file.name
                )

    # ========================================================
    # FINAL ZIP VALIDATION
    # ========================================================

    try:

        with zipfile.ZipFile(
            OUTPUT_ZIP,
            "r"
        ) as zf:

            names = zf.namelist()

            if "queue.json" not in names:

                fail(
                    "queue.json missing from ZIP."
                )

            image_count = len(
                [
                    name
                    for name in names
                    if name.lower().endswith(
                        (
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp"
                        )
                    )
                ]
            )

            if image_count != len(tasks):

                fail(
                    "Image/task count mismatch: "
                    f"{image_count} images / "
                    f"{len(tasks)} tasks."
                )

            bad = zf.testzip()

            if bad:

                fail(
                    f"ZIP integrity failure: {bad}"
                )

    except zipfile.BadZipFile as exc:

        fail(
            f"Invalid ZIP: {exc}"
        )

    # ========================================================
    # DONE
    # ========================================================

    size_mb = (
        OUTPUT_ZIP.stat().st_size
        /
        (1024 * 1024)
    )

    print()
    print("=" * 64)
    print("WAN2GP TEST 7 QUEUE READY")
    print("=" * 64)

    print(
        "Tasks  :",
        len(tasks)
    )

    print(
        "Images :",
        image_count
    )

    print(
        "ZIP    :",
        f"{size_mb:.2f} MB"
    )

    print(
        "File   :",
        OUTPUT_ZIP
    )

    print()
    print(
        "TEST 7 BASELINE LOCKED:"
    )

    print(
        "480x832 | 33 frames | 16 FPS | "
        "16 steps | motion 0.55"
    )

    print()
    print(
        "READY FOR WAN2GP T4 WORKER"
    )

    print("=" * 64)


if __name__ == "__main__":
    main()