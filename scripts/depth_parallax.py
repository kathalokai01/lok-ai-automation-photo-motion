#!/usr/bin/env python3
"""Render cinematic depth-guided 2.5D Photo Motion clips."""

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

VERSION = "depth-parallax-v5-stable-cinematic"

MIN_BYTES = 1000
MIN_DIMENSION = 64
MAX_DIMENSION = 4096
MAX_FPS = 60
MAX_SECONDS = 600

DEFAULT_STRENGTH = 0.026
MAX_STRENGTH = 0.04

MIN_ZOOM = 1.045
MAX_ZOOM = 1.095
ZOOM_VARIATION = 0.025

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
    """Apply exactly the same crop and resize to RGB and depth."""

    source_height, source_width = image.shape[:2]

    if depth.shape[:2] != image.shape[:2]:
        depth = cv2.resize(
            depth,
            (source_width, source_height),
            interpolation=cv2.INTER_LINEAR,
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
        interpolation=cv2.INTER_LINEAR,
    )

    if image_out.shape[:2] != depth_out.shape[:2]:
        raise RuntimeError("Image and depth alignment failed.")

    return image_out, depth_out


def normalize_depth(depth):
    """Normalize depth while suppressing outlier pixels and noise."""

    values = np.asarray(depth, dtype=np.float32)

    if values.ndim != 2 or not np.isfinite(values).all():
        raise RuntimeError("Depth map contains invalid pixel values.")

    low = float(np.percentile(values, 2))
    high = float(np.percentile(values, 98))

    if high - low < 1.0:
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
        sigmaX=2.0,
        sigmaY=2.0,
    )

    return np.clip(normalized, 0.0, 1.0)


def get_scene_motion_seed(image_path, depth_path):
    """Stable scene-specific movement based on actual source assets."""

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
            "name": "cinematic_left",
            "direction": (-1.0, 0.0),
            "cross": 0.16,
            "phase": 0.0,
        },
        {
            "name": "cinematic_right",
            "direction": (1.0, 0.0),
            "cross": -0.16,
            "phase": 0.7,
        },
        {
            "name": "vertical_reveal",
            "direction": (0.0, -1.0),
            "cross": 0.14,
            "phase": 1.2,
        },
        {
            "name": "vertical_rise",
            "direction": (0.0, 1.0),
            "cross": -0.14,
            "phase": 1.8,
        },
        {
            "name": "diagonal_left",
            "direction": (-0.707, -0.707),
            "cross": 0.16,
            "phase": 2.4,
        },
        {
            "name": "diagonal_right",
            "direction": (0.707, -0.707),
            "cross": -0.16,
            "phase": 3.0,
        },
        {
            "name": "depth_push",
            "direction": (0.707, 0.707),
            "cross": 0.12,
            "phase": 3.6,
        },
        {
            "name": "depth_pull",
            "direction": (-0.707, 0.707),
            "cross": -0.12,
            "phase": 4.2,
        },
    ]

    return profiles[seed % len(profiles)]


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def calculate_camera_transform(
    progress,
    width,
    height,
    strength,
    profile,
):
    """Smooth camera movement with restrained zoom and depth drift."""

    progress = max(0.0, min(1.0, float(progress)))
    phase = 2.0 * math.pi * progress
    eased = smoothstep(progress)

    direction_x, direction_y = profile["direction"]
    phase_offset = profile["phase"]

    zoom_wave = 0.5 + 0.5 * math.sin(
        phase + phase_offset - math.pi / 2.0
    )

    zoom = MIN_ZOOM + ZOOM_VARIATION * zoom_wave
    zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))

    pan_x_limit = width * strength * 1.10
    pan_y_limit = height * strength * 1.10

    primary_x = math.sin(phase) * pan_x_limit * 0.48
    primary_y = math.sin(phase + 0.75) * pan_y_limit * 0.48

    directional_x = (
        direction_x * pan_x_limit * 0.62
        * math.sin(math.pi * eased)
    )
    directional_y = (
        direction_y * pan_y_limit * 0.62
        * math.sin(math.pi * eased)
    )

    cross = float(profile["cross"])

    drift_x = (
        primary_x
        + directional_x
        + math.sin(phase * 0.5 + phase_offset)
        * pan_x_limit * cross
    )
    drift_y = (
        primary_y
        + directional_y
        + math.cos(phase * 0.5 + phase_offset)
        * pan_y_limit * cross
    )

    return {
        "zoom": zoom,
        "pan_x": drift_x,
        "pan_y": drift_y,
        "depth_x": direction_x,
        "depth_y": direction_y,
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
    """Build a single aligned RGB remap for the current camera frame."""

    zoom = camera["zoom"]

    zoom_x = (width - width / zoom) / 2.0
    zoom_y = (height - height / zoom) / 2.0

    map_x = (base_x - zoom_x) / zoom
    map_y = (base_y - zoom_y) / zoom

    depth_x = (
        camera["depth_x"]
        * strength
        * width
        * 1.25
        * displacement
    )
    depth_y = (
        camera["depth_y"]
        * strength
        * height
        * 1.25
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

    for frame_index in range(args.frames):
        progress = frame_index / max(args.frames - 1, 1)

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
                f"Rendered {frame_index + 1}/{args.frames} frames",
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

        for position in (0, frame_count // 2, frame_count - 1):
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            success, frame = capture.read()

            if not success or frame is None or frame.size == 0:
                raise RuntimeError(
                    f"Cannot decode video frame {position}."
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
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
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
            "H.264 encoding failed: " + result.stderr[-2000:]
        )

    if not destination.is_file() or destination.stat().st_size < MIN_BYTES:
        destination.unlink(missing_ok=True)
        raise RuntimeError("H.264 output is missing or too small.")

    return True


def render_video(args, image_path, depth_path, output_path):
    image = cv2.imread(
        str(image_path),
        cv2.IMREAD_COLOR,
    )
    depth = cv2.imread(
        str(depth_path),
        cv2.IMREAD_UNCHANGED,
    )

    if image is None:
        raise RuntimeError("Source image could not be decoded.")

    if depth is None:
        raise RuntimeError("Depth map could not be decoded.")

    if image.ndim != 3 or image.shape[2] != 3:
        raise RuntimeError("Source image must be a valid RGB/BGR image.")

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
        image.shape[0] < MIN_DIMENSION
        or image.shape[1] < MIN_DIMENSION
    ):
        raise RuntimeError("Source image dimensions are too small.")

    if (
        depth.shape[0] < 16
        or depth.shape[1] < 16
    ):
        raise RuntimeError("Depth map dimensions are too small.")

    image, depth = apply_shared_crop(
        image,
        depth,
        args.width,
        args.height,
    )

    if not np.isfinite(depth).all():
        raise RuntimeError("Depth map contains non-finite values.")

    seed = get_scene_motion_seed(image_path, depth_path)
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
            "Render smooth cinematic depth-guided 2.5D Photo Motion."
        )
    )

    # These arguments match the existing photo_motion.py caller.
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
        parser.error("Maximum supported scene duration is 600 seconds.")

    try:
        validate_dimensions(args.width, args.height)
    except RuntimeError as exc:
        parser.error(str(exc))

    image_path = Path(args.image).resolve()
    depth_path = Path(args.depth).resolve()
    output_path = Path(args.output).resolve()

    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")

    if not depth_path.is_file():
        raise FileNotFoundError(f"Depth map not found: {depth_path}")

    if output_path in {image_path, depth_path}:
        raise RuntimeError("Output must differ from both input files.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

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
        print(f"DEPTH PARALLAX FAILED: {exc}", file=sys.stderr)
        sys.exit(1)