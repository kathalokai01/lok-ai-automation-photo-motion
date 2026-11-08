#!/usr/bin/env python3
"""Depth-guided cinematic 2.5D Photo Motion renderer."""

from __future__ import annotations

import argparse
import hashlib
import math
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

VERSION = "depth-parallax-v9-faster-stronger-variable-camera-motion"

MIN_BYTES = 1000
MIN_DIMENSION = 64
MAX_DIMENSION = 4096
MAX_FPS = 60
MAX_SECONDS = 600

DEFAULT_STRENGTH = 0.045
MAX_STRENGTH = 0.055

MIN_ZOOM = 1.025
MAX_ZOOM = 1.22
BORDER_MODE = cv2.BORDER_REFLECT_101


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Value must be a positive integer."
        ) from exc

    if number <= 0:
        raise argparse.ArgumentTypeError(
            "Value must be greater than zero."
        )

    return number


def bounded_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Value must be numeric."
        ) from exc

    if not math.isfinite(number) or not 0 <= number <= MAX_STRENGTH:
        raise argparse.ArgumentTypeError(
            f"Value must be between 0 and {MAX_STRENGTH}."
        )

    return number


def validate_dimensions(width, height):
    if not MIN_DIMENSION <= width <= MAX_DIMENSION:
        raise RuntimeError(
            f"Width must be between {MIN_DIMENSION} and {MAX_DIMENSION}."
        )

    if not MIN_DIMENSION <= height <= MAX_DIMENSION:
        raise RuntimeError(
            f"Height must be between {MIN_DIMENSION} and {MAX_DIMENSION}."
        )


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_source_file(path, label):
    path = Path(path)

    if not path.is_file():
        raise RuntimeError(f"{label} does not exist: {path}")

    if path.stat().st_size < MIN_BYTES:
        raise RuntimeError(f"{label} is empty or too small: {path}")

    return path


def center_crop_parameters(
    source_width,
    source_height,
    target_width,
    target_height,
):
    source_ratio = source_width / source_height
    target_ratio = target_width / target_height

    if source_ratio > target_ratio:
        crop_height = source_height
        crop_width = max(
            1,
            round(source_height * target_ratio),
        )
        left = (source_width - crop_width) // 2
        top = 0
    else:
        crop_width = source_width
        crop_height = max(
            1,
            round(source_width / target_ratio),
        )
        left = 0
        top = (source_height - crop_height) // 2

    return left, top, crop_width, crop_height


def apply_shared_crop(
    image,
    depth,
    target_width,
    target_height,
):
    """Use exactly the same crop and resize for image and depth."""

    source_height, source_width = image.shape[:2]

    if depth.shape[:2] != image.shape[:2]:
        depth = cv2.resize(
            depth,
            (source_width, source_height),
            interpolation=cv2.INTER_CUBIC,
        )

    left, top, crop_width, crop_height = center_crop_parameters(
        source_width,
        source_height,
        target_width,
        target_height,
    )

    image_crop = image[
        top:top + crop_height,
        left:left + crop_width,
    ]

    depth_crop = depth[
        top:top + crop_height,
        left:left + crop_width,
    ]

    if image_crop.size == 0 or depth_crop.size == 0:
        raise RuntimeError("Image/depth crop produced an empty frame.")

    image_out = cv2.resize(
        image_crop,
        (target_width, target_height),
        interpolation=cv2.INTER_LANCZOS4,
    )

    depth_out = cv2.resize(
        depth_crop,
        (target_width, target_height),
        interpolation=cv2.INTER_CUBIC,
    )

    if image_out.shape[:2] != depth_out.shape[:2]:
        raise RuntimeError("Image and depth alignment failed.")

    return image_out, depth_out


def normalize_depth(depth):
    """Normalize depth robustly while suppressing extreme outliers."""

    values = np.asarray(depth, dtype=np.float32)

    if values.ndim != 2:
        raise RuntimeError("Depth map must contain one channel.")

    if not np.isfinite(values).all():
        raise RuntimeError("Depth map contains non-finite values.")

    low = float(np.percentile(values, 2))
    high = float(np.percentile(values, 98))

    if high - low < 1e-6:
        normalized = np.full(values.shape, 0.5, dtype=np.float32)
    else:
        normalized = np.clip(
            (values - low) / (high - low),
            0.0,
            1.0,
        ).astype(np.float32)

    normalized = cv2.GaussianBlur(
        normalized,
        (0, 0),
        sigmaX=1.6,
        sigmaY=1.6,
    )

    return np.clip(normalized, 0.0, 1.0)


def get_scene_motion_seed(image_path, depth_path):
    """Stable motion seed: same source assets always produce same motion."""

    digest = hashlib.sha256()

    for path in (image_path, depth_path):
        digest.update(sha256_file(path).encode("ascii"))

    return int.from_bytes(
        digest.digest()[:8],
        byteorder="big",
        signed=False,
    )


def make_motion_profile(seed):
    profiles = [
        {
            "name": "cinematic_left_reveal",
            "direction": (-1.0, 0.0),
            "cross": 0.12,
            "phase": 0.0,
            "roll": -0.25,
            "zoom_bias": 0.10,
        },
        {
            "name": "cinematic_right_reveal",
            "direction": (1.0, 0.0),
            "cross": -0.12,
            "phase": 0.7,
            "roll": 0.25,
            "zoom_bias": 0.15,
        },
        {
            "name": "vertical_rise",
            "direction": (0.0, -1.0),
            "cross": 0.14,
            "phase": 1.2,
            "roll": 0.12,
            "zoom_bias": 0.20,
        },
        {
            "name": "vertical_fall",
            "direction": (0.0, 1.0),
            "cross": -0.14,
            "phase": 1.8,
            "roll": -0.12,
            "zoom_bias": 0.05,
        },
        {
            "name": "diagonal_push_left",
            "direction": (-0.707, -0.707),
            "cross": 0.15,
            "phase": 2.4,
            "roll": -0.30,
            "zoom_bias": 0.25,
        },
        {
            "name": "diagonal_push_right",
            "direction": (0.707, -0.707),
            "cross": -0.15,
            "phase": 3.0,
            "roll": 0.30,
            "zoom_bias": 0.18,
        },
        {
            "name": "depth_push",
            "direction": (0.707, 0.707),
            "cross": 0.11,
            "phase": 3.6,
            "roll": 0.10,
            "zoom_bias": 0.30,
        },
        {
            "name": "depth_pull",
            "direction": (-0.707, 0.707),
            "cross": -0.11,
            "phase": 4.2,
            "roll": -0.10,
            "zoom_bias": 0.00,
        },
        {
            "name": "curved_orbit_left",
            "direction": (-0.45, 0.89),
            "cross": 0.18,
            "phase": 4.8,
            "roll": -0.34,
            "zoom_bias": 0.28,
        },
        {
            "name": "curved_orbit_right",
            "direction": (0.45, 0.89),
            "cross": -0.18,
            "phase": 5.4,
            "roll": 0.34,
            "zoom_bias": 0.22,
        },
        {
            "name": "center_push",
            "direction": (0.0, -1.0),
            "cross": 0.20,
            "phase": 5.9,
            "roll": 0.18,
            "zoom_bias": 0.35,
        },
        {
            "name": "center_pull",
            "direction": (0.0, 1.0),
            "cross": -0.20,
            "phase": 6.4,
            "roll": -0.18,
            "zoom_bias": 0.02,
        },
    ]

    return profiles[seed % len(profiles)]


def smoothstep(value):
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def smootherstep(value):
    value = max(0.0, min(1.0, float(value)))
    return value * value * value * (
        value * (value * 6.0 - 15.0) + 10.0
    )


def calculate_camera_transform(
    progress,
    width,
    height,
    strength,
    profile,
):
    """Create continuous camera movement without abrupt scene jumps."""

    progress = max(0.0, min(1.0, float(progress)))
    phase = 2.0 * math.pi * progress
    eased = smootherstep(progress)

    direction_x, direction_y = profile["direction"]
    phase_offset = profile["phase"]

    zoom_wave = 0.5 - 0.5 * math.cos(
        math.pi * eased
    )

    zoom_start = MIN_ZOOM + profile["zoom_bias"] * 0.012
    zoom_end = MAX_ZOOM - profile["zoom_bias"] * 0.010

    zoom = zoom_start + (zoom_end - zoom_start) * zoom_wave

    breathing = (
        math.sin(phase + phase_offset)
        * 0.006
        * math.sin(math.pi * progress)
    )

    zoom = max(
        MIN_ZOOM,
        min(MAX_ZOOM, zoom + breathing),
    )

    pan_x_limit = width * strength * 1.25
    pan_y_limit = height * strength * 1.25

    primary_x = (
        math.sin(math.pi * eased)
        * direction_x
        * pan_x_limit
        * 0.82
    )

    primary_y = (
        math.sin(math.pi * eased)
        * direction_y
        * pan_y_limit
        * 0.72
    )

    secondary_x = (
        math.sin(phase * 0.5 + phase_offset)
        * pan_x_limit
        * float(profile["cross"])
    )

    secondary_y = (
        math.cos(phase * 0.5 + phase_offset)
        * pan_y_limit
        * float(profile["cross"])
    )

    micro_x = (
        math.sin(phase * 1.5 + phase_offset)
        * pan_x_limit
        * 0.05
    )

    micro_y = (
        math.cos(phase * 1.25 + phase_offset)
        * pan_y_limit
        * 0.035
    )

    roll = (
        float(profile["roll"])
        * math.sin(math.pi * eased)
        * 0.24
    )

    return {
        "zoom": zoom,
        "pan_x": primary_x + secondary_x + micro_x,
        "pan_y": primary_y + secondary_y + micro_y,
        "depth_x": direction_x,
        "depth_y": direction_y,
        "roll": roll,
    }


def build_remap(
    base_x,
    base_y,
    displacement,
    camera,
    width,
    height,
    strength,
):
    """Build depth displacement and camera transform maps."""

    zoom = camera["zoom"]

    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0

    relative_x = base_x - center_x
    relative_y = base_y - center_y

    angle = math.radians(camera["roll"])
    cosine = math.cos(angle)
    sine = math.sin(angle)

    rotated_x = cosine * relative_x - sine * relative_y
    rotated_y = sine * relative_x + cosine * relative_y

    map_x = center_x + rotated_x / zoom
    map_y = center_y + rotated_y / zoom

    depth_x = (
        camera["depth_x"]
        * strength
        * width
        * 1.65
        * displacement
    )

    depth_y = (
        camera["depth_y"]
        * strength
        * height
        * 1.45
        * displacement
    )

    map_x = map_x - camera["pan_x"] - depth_x
    map_y = map_y - camera["pan_y"] - depth_y

    return (
        np.ascontiguousarray(map_x, dtype=np.float32),
        np.ascontiguousarray(map_y, dtype=np.float32),
    )


def render_frames(image, depth, args, writer, profile):
    height, width = image.shape[:2]

    depth_normalized = normalize_depth(depth)
    displacement = (depth_normalized - 0.5) * 2.0

    base_x, base_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )

    total = args.frames

    for frame_index in range(total):
        progress = (
            frame_index / max(total - 1, 1)
        )

        camera = calculate_camera_transform(
            progress,
            width,
            height,
            args.strength,
            profile,
        )

        map_x, map_y = build_remap(
            base_x,
            base_y,
            displacement,
            camera,
            width,
            height,
            args.strength,
        )

        frame = cv2.remap(
            image,
            map_x,
            map_y,
            interpolation=cv2.INTER_CUBIC,
            borderMode=BORDER_MODE,
        )

        if frame is None or frame.shape != image.shape:
            raise RuntimeError(
                f"Invalid rendered frame: {frame_index + 1}."
            )

        writer.write(frame)

        if not writer.isOpened():
            raise RuntimeError("Video writer stopped unexpectedly.")

        if (frame_index + 1) % args.fps == 0:
            print(
                f"Rendered {frame_index + 1}/{total} frames",
                flush=True,
            )

def validate_video(path, width, height, expected_frames, fps):
    path = Path(path)

    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError("Rendered video is missing or too small.")

    capture = cv2.VideoCapture(str(path))

    try:
        if not capture.isOpened():
            raise RuntimeError("Cannot reopen rendered video.")

        actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        actual_fps = float(capture.get(cv2.CAP_PROP_FPS))

        if (actual_width, actual_height) != (width, height):
            raise RuntimeError(
                f"Resolution mismatch: {actual_width}x{actual_height}; "
                f"expected {width}x{height}."
            )

        if frame_count != expected_frames:
            raise RuntimeError(
                f"Frame count mismatch: {frame_count}; "
                f"expected {expected_frames}."
            )

        if not math.isfinite(actual_fps) or actual_fps <= 0:
            raise RuntimeError("Video FPS is invalid.")

        if abs(actual_fps - fps) > 1.0:
            raise RuntimeError(
                f"FPS mismatch: {actual_fps}; expected {fps}."
            )

        positions = sorted({
            0,
            frame_count // 4,
            frame_count // 2,
            (frame_count * 3) // 4,
            frame_count - 1,
        })

        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            success, frame = capture.read()

            if (
                not success
                or frame is None
                or frame.size == 0
                or frame.shape[:2] != (height, width)
            ):
                raise RuntimeError(
                    f"Cannot decode rendered frame {position}."
                )

    finally:
        capture.release()


def encode_h264(source, destination, fps, expected_frames):
    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        return False

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(source),
        "-map", "0:v:0",
        "-an",
        "-frames:v", str(expected_frames),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(destination),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            "H.264 encoding failed: " + result.stderr[-2500:]
        )

    if not destination.is_file() or destination.stat().st_size < MIN_BYTES:
        destination.unlink(missing_ok=True)
        raise RuntimeError("H.264 output is missing or too small.")

    return True


def load_source_image(path):
    image = cv2.imread(
        str(path),
        cv2.IMREAD_COLOR,
    )

    if image is None:
        raise RuntimeError("Source image could not be decoded.")

    if image.ndim != 3 or image.shape[2] != 3:
        raise RuntimeError("Source image must have three color channels.")

    height, width = image.shape[:2]
    validate_dimensions(width, height)

    if not np.isfinite(image).all():
        raise RuntimeError("Source image contains invalid pixel values.")

    return image


def load_source_depth(path):
    depth = cv2.imread(
        str(path),
        cv2.IMREAD_UNCHANGED,
    )

    if depth is None:
        raise RuntimeError("Depth map could not be decoded.")

    if depth.ndim == 3:
        if depth.shape[2] == 4:
            depth = cv2.cvtColor(
                depth,
                cv2.COLOR_BGRA2GRAY,
            )
        else:
            depth = cv2.cvtColor(
                depth,
                cv2.COLOR_BGR2GRAY,
            )

    if depth.ndim != 2:
        raise RuntimeError("Depth map must be a single-channel image.")

    if (
        depth.shape[0] < 16
        or depth.shape[1] < 16
    ):
        raise RuntimeError("Depth map dimensions are too small.")

    if not np.isfinite(depth).all():
        raise RuntimeError("Depth map contains invalid pixel values.")

    if float(np.max(depth)) - float(np.min(depth)) <= 0:
        raise RuntimeError("Depth map contains no usable depth variation.")

    return depth


def render_video(args, image_path, depth_path, output_path):
    image = load_source_image(image_path)
    depth = load_source_depth(depth_path)

    image, depth = apply_shared_crop(
        image,
        depth,
        args.width,
        args.height,
    )

    seed = get_scene_motion_seed(
        image_path,
        depth_path,
    )
    profile = make_motion_profile(seed)

    temporary = output_path.with_name(
        output_path.stem + ".temporary.mp4"
    )
    converted = output_path.with_name(
        output_path.stem + ".converted.mp4"
    )

    if temporary in {image_path, depth_path, output_path}:
        raise RuntimeError("Temporary video path conflicts with input.")

    if converted in {image_path, depth_path, output_path}:
        raise RuntimeError("Converted video path conflicts with input.")

    temporary.unlink(missing_ok=True)
    converted.unlink(missing_ok=True)

    writer = cv2.VideoWriter(
        str(temporary),
        cv2.VideoWriter_fourcc(*"mp4v"),
        args.fps,
        (args.width, args.height),
    )

    if not writer.isOpened():
        writer.release()
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Could not initialize the video writer.")

    try:
        render_frames(
            image,
            depth,
            args,
            writer,
            profile,
        )

    except Exception:
        writer.release()
        temporary.unlink(missing_ok=True)
        raise

    finally:
        writer.release()

    if not temporary.is_file() or temporary.stat().st_size < MIN_BYTES:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Temporary rendered video is invalid.")

    try:
        validate_video(
            temporary,
            args.width,
            args.height,
            args.frames,
            args.fps,
        )

        if encode_h264(
            temporary,
            converted,
            args.fps,
            args.frames,
        ):
            validate_video(
                converted,
                args.width,
                args.height,
                args.frames,
                args.fps,
            )

            converted.replace(output_path)
            temporary.unlink(missing_ok=True)

        else:
            temporary.replace(output_path)

        validate_video(
            output_path,
            args.width,
            args.height,
            args.frames,
            args.fps,
        )

    except Exception:
        temporary.unlink(missing_ok=True)
        converted.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        raise

    return profile


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Render cinematic depth-guided 2.5D Photo Motion clips."
        )
    )

    # CLI compatibility with scripts/photo_motion.py is preserved.
    parser.add_argument("--image", required=True)
    parser.add_argument("--depth", required=True)
    parser.add_argument("--output", required=True)

    parser.add_argument(
        "--width",
        type=positive_int,
        default=720,
    )
    parser.add_argument(
        "--height",
        type=positive_int,
        default=1280,
    )
    parser.add_argument(
        "--fps",
        type=positive_int,
        default=24,
    )
    parser.add_argument(
        "--frames",
        type=positive_int,
        default=144,
    )
    parser.add_argument(
        "--strength",
        type=bounded_float,
        default=DEFAULT_STRENGTH,
    )

    args = parser.parse_args()

    if args.fps > MAX_FPS:
        parser.error(f"--fps cannot exceed {MAX_FPS}.")

    if args.frames > args.fps * MAX_SECONDS:
        parser.error(
            "Maximum supported scene duration is 600 seconds."
        )

    try:
        validate_dimensions(args.width, args.height)
    except RuntimeError as exc:
        parser.error(str(exc))

    image_path = validate_source_file(
        args.image,
        "Source image",
    )
    depth_path = validate_source_file(
        args.depth,
        "Depth map",
    )

    output_path = Path(args.output).resolve()

    if output_path in {image_path, depth_path}:
        raise RuntimeError(
            "Output path must differ from both source files."
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    profile = render_video(
        args,
        image_path,
        depth_path,
        output_path,
    )

    print("=" * 55)
    print("DEPTH PARALLAX SUCCESS")
    print("Version:", VERSION)
    print("Movement profile:", profile["name"])
    print("Strength:", args.strength)
    print("Output:", output_path)
    print("Frames:", args.frames)
    print("FPS:", args.fps)
    print("Resolution:", f"{args.width}x{args.height}")
    print("Duration:", f"{args.frames / args.fps:.3f}s")
    print("Size bytes:", output_path.stat().st_size)
    print("SHA256:", sha256_file(output_path))
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("DEPTH PARALLAX CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(
            f"DEPTH PARALLAX FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)