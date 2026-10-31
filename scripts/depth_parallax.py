#!/usr/bin/env python3
"""Validated, smooth and dynamic depth-guided 2.5D parallax renderer."""

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

VERSION = "depth-parallax-v4-dynamic-cinematic-motion"

MIN_BYTES = 1000
MAX_FPS = 60
MAX_SECONDS = 600
MIN_DIMENSION = 64
MAX_DIMENSION = 4096

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
            "Must be a positive integer."
        ) from exc

    if number <= 0:
        raise argparse.ArgumentTypeError(
            "Must be greater than zero."
        )

    return number


def bounded_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Must be a number."
        ) from exc

    if (
        not math.isfinite(number)
        or not 0.0 <= number <= MAX_STRENGTH
    ):
        raise argparse.ArgumentTypeError(
            f"Must be between 0 and {MAX_STRENGTH}."
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


def validate_video(path, width, height, expected_frames, fps):
    path = Path(path)

    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError(
            "Output video is missing or too small."
        )

    cap = cv2.VideoCapture(str(path))

    try:
        if not cap.isOpened():
            raise RuntimeError(
                "Cannot reopen generated video."
            )

        actual_width = int(
            cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        )
        actual_height = int(
            cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        )
        frame_count = int(
            cap.get(cv2.CAP_PROP_FRAME_COUNT)
        )
        actual_fps = float(
            cap.get(cv2.CAP_PROP_FPS)
        )

        if (actual_width, actual_height) != (width, height):
            raise RuntimeError(
                f"Wrong dimensions: {actual_width}x{actual_height}; "
                f"expected {width}x{height}."
            )

        if frame_count != expected_frames:
            raise RuntimeError(
                f"Frame count is {frame_count}; "
                f"expected {expected_frames}."
            )

        if not math.isfinite(actual_fps) or actual_fps <= 0:
            raise RuntimeError(
                "Video FPS could not be validated."
            )

        if abs(actual_fps - fps) > 1.0:
            raise RuntimeError(
                f"Unexpected FPS: {actual_fps}; expected {fps}."
            )

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok_first, first = cap.read()

        cap.set(
            cv2.CAP_PROP_POS_FRAMES,
            frame_count - 1,
        )
        ok_last, last = cap.read()

        if not ok_first or first is None:
            raise RuntimeError(
                "Could not decode first output frame."
            )

        if not ok_last or last is None:
            raise RuntimeError(
                "Could not decode last output frame."
            )

    finally:
        cap.release()


def center_crop_parameters(
    source_width,
    source_height,
    target_width,
    target_height,
):
    """Return one crop transform shared by RGB and depth images."""

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
    """Align depth to the original image before applying the same crop."""

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
        raise RuntimeError(
            "Shared image/depth crop produced empty output."
        )

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
        raise RuntimeError(
            "Image/depth alignment failed."
        )

    return image_out, depth_out


def normalize_depth(depth):
    """Normalize depth robustly while reducing extreme-value noise."""

    depth_float = depth.astype(np.float32)

    low = float(np.percentile(depth_float, 2))
    high = float(np.percentile(depth_float, 98))

    if not math.isfinite(low) or not math.isfinite(high):
        raise RuntimeError(
            "Depth map contains invalid values."
        )

    if high - low < 1.0:
        normalized = np.full(
            depth_float.shape,
            0.5,
            dtype=np.float32,
        )
    else:
        normalized = (
            (depth_float - low) / (high - low)
        )
        normalized = np.clip(
            normalized,
            0.0,
            1.0,
        )

    normalized = cv2.GaussianBlur(
        normalized,
        (0, 0),
        2.2,
    )

    return normalized


def get_scene_motion_seed(image_path, depth_path):
    """Generate a repeatable movement direction for this scene."""

    digest = hashlib.sha256()

    for path in (image_path, depth_path):
        with Path(path).open("rb") as stream:
            for block in iter(
                lambda: stream.read(1024 * 1024),
                b"",
            ):
                digest.update(block)

    return int.from_bytes(
        digest.digest()[:8],
        byteorder="big",
        signed=False,
    )


def make_motion_profile(seed):
    """Select a deterministic but scene-specific camera movement path."""

    profiles = [
        {
            "name": "horizontal_left",
            "direction": (-1.0, 0.0),
            "cross": 0.22,
            "zoom_phase": 0.0,
        },
        {
            "name": "horizontal_right",
            "direction": (1.0, 0.0),
            "cross": -0.22,
            "zoom_phase": 0.7,
        },
        {
            "name": "vertical_up",
            "direction": (0.0, -1.0),
            "cross": 0.18,
            "zoom_phase": 1.2,
        },
        {
            "name": "vertical_down",
            "direction": (0.0, 1.0),
            "cross": -0.18,
            "zoom_phase": 1.8,
        },
        {
            "name": "diagonal_up_left",
            "direction": (-0.707, -0.707),
            "cross": 0.20,
            "zoom_phase": 2.4,
        },
        {
            "name": "diagonal_up_right",
            "direction": (0.707, -0.707),
            "cross": -0.20,
            "zoom_phase": 3.0,
        },
        {
            "name": "diagonal_down_left",
            "direction": (-0.707, 0.707),
            "cross": -0.20,
            "zoom_phase": 3.6,
        },
        {
            "name": "diagonal_down_right",
            "direction": (0.707, 0.707),
            "cross": 0.20,
            "zoom_phase": 4.2,
        },
    ]

    return profiles[seed % len(profiles)]


def smoothstep(value):
    """Smooth easing with zero velocity at both ends."""

    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def calculate_camera_transform(
    progress,
    width,
    height,
    strength,
    profile,
):
    """
    Calculate a smooth camera path with zoom, pan and depth displacement.

    Camera motion is deliberately stronger than the earlier renderer,
    while the crop reserve prevents exposed borders.
    """

    direction_x, direction_y = profile["direction"]

    phase = 2.0 * math.pi * progress
    eased = smoothstep(progress)

    zoom_phase = profile["zoom_phase"]

    zoom_wave = (
        0.5
        + 0.5 * math.sin(
            phase + zoom_phase - math.pi / 2.0
        )
    )

    zoom = (
        MIN_ZOOM
        + ZOOM_VARIATION * zoom_wave
    )

    zoom = min(
        MAX_ZOOM,
        max(MIN_ZOOM, zoom),
    )

    max_pan_x = width * strength * 1.15
    max_pan_y = height * strength * 1.15

    primary_x = (
        math.sin(phase) * max_pan_x
    )
    primary_y = (
        math.sin(phase + 0.75) * max_pan_y
    )

    diagonal_x = (
        direction_x * max_pan_x * 0.75
    )
    diagonal_y = (
        direction_y * max_pan_y * 0.75
    )

    drift_x = (
        primary_x * 0.55
        + diagonal_x * math.sin(math.pi * eased)
    )

    drift_y = (
        primary_y * 0.55
        + diagonal_y * math.sin(math.pi * eased)
    )

    cross = float(profile["cross"])

    drift_x += (
        math.sin(phase * 0.5 + zoom_phase)
        * max_pan_x
        * cross
    )

    drift_y += (
        math.cos(phase * 0.5 + zoom_phase)
        * max_pan_y
        * cross
    )

    return {
        "zoom": zoom,
        "pan_x": drift_x,
        "pan_y": drift_y,
        "depth_x": direction_x,
        "depth_y": direction_y,
    }


def render_frames(image, depth, args, writer, profile):
    height, width = image.shape[:2]

    depth_normalized = normalize_depth(depth)

    displacement = (
        depth_normalized - 0.5
    ) * 2.0

    base_x, base_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )

    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0

    for frame_index in range(args.frames):
        progress = (
            frame_index / max(args.frames - 1, 1)
        )

        camera = calculate_camera_transform(
            progress,
            width,
            height,
            args.strength,
            profile,
        )

        zoom = camera["zoom"]

        zoom_x = (
            width - width / zoom
        ) / 2.0

        zoom_y = (
            height - height / zoom
        ) / 2.0

        map_x_base = (
            base_x - zoom_x
        ) / zoom

        map_y_base = (
            base_y - zoom_y
        ) / zoom

        # Smooth camera movement across the entire scene.
        camera_x = camera["pan_x"]
        camera_y = camera["pan_y"]

        # Depth separation creates foreground/background parallax.
        depth_shift_x = (
            camera["depth_x"]
            * args.strength
            * width
            * 1.35
            * displacement
        )

        depth_shift_y = (
            camera["depth_y"]
            * args.strength
            * height
            * 1.35
            * displacement
        )

        map_x = (
            map_x_base
            - camera_x
            - depth_shift_x
        ).astype(np.float32)

        map_y = (
            map_y_base
            - camera_y
            - depth_shift_y
        ).astype(np.float32)

        frame = cv2.remap(
            image,
            map_x,
            map_y,
            interpolation=cv2.INTER_CUBIC,
            borderMode=BORDER_MODE,
        )

        if frame is None or frame.shape != image.shape:
            raise RuntimeError(
                f"Invalid frame generated at {frame_index + 1}."
            )

        writer.write(frame)

        if not writer.isOpened():
            raise RuntimeError(
                "Video writer stopped unexpectedly."
            )

        if (frame_index + 1) % args.fps == 0:
            print(
                f"Rendered {frame_index + 1}/{args.frames} frames",
                flush=True,
            )


def encode_h264(
    source,
    destination,
    fps,
    expected_frames,
):
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

    if result.returncode:
        destination.unlink(missing_ok=True)

        raise RuntimeError(
            "FFmpeg H.264 conversion failed: "
            + result.stderr[-2000:]
        )

    if (
        not destination.is_file()
        or destination.stat().st_size < MIN_BYTES
    ):
        destination.unlink(missing_ok=True)

        raise RuntimeError(
            "Converted H.264 video is invalid."
        )

    return True


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Render smooth, dynamic depth-guided "
            "2.5D cinematic parallax."
        )
    )

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
        parser.error(
            f"--fps must not exceed {MAX_FPS}."
        )

    try:
        validate_dimensions(
            args.width,
            args.height,
        )
    except RuntimeError as exc:
        parser.error(str(exc))

    if args.frames > args.fps * MAX_SECONDS:
        parser.error(
            "--frames cannot exceed ten minutes of video."
        )

    image_path = Path(args.image).resolve()
    depth_path = Path(args.depth).resolve()
    output_path = Path(args.output).resolve()

    if not image_path.is_file():
        raise FileNotFoundError(
            f"Image not found: {image_path}"
        )

    if not depth_path.is_file():
        raise FileNotFoundError(
            f"Depth map not found: {depth_path}"
        )

    if output_path in (image_path, depth_path):
        raise RuntimeError(
            "Output must differ from both input files."
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = output_path.with_name(
        output_path.stem + ".temporary.mp4"
    )

    converted = output_path.with_name(
        output_path.stem + ".converted.mp4"
    )

    if temporary in (image_path, depth_path):
        raise RuntimeError(
            "Temporary output conflicts with an input file."
        )

    if converted in (image_path, depth_path):
        raise RuntimeError(
            "Converted output conflicts with an input file."
        )

    image = cv2.imread(
        str(image_path),
        cv2.IMREAD_COLOR,
    )

    depth = cv2.imread(
        str(depth_path),
        cv2.IMREAD_GRAYSCALE,
    )

    if image is None:
        raise RuntimeError(
            "Could not decode source image."
        )

    if depth is None:
        raise RuntimeError(
            "Could not decode depth map."
        )

    if (
        image.shape[0] < MIN_DIMENSION
        or image.shape[1] < MIN_DIMENSION
    ):
        raise RuntimeError(
            "Source image is too small."
        )

    if (
        depth.shape[0] < 16
        or depth.shape[1] < 16
    ):
        raise RuntimeError(
            "Depth map is too small."
        )

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

        raise RuntimeError(
            "Could not initialize MP4 video writer."
        )

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

    if (
        not temporary.is_file()
        or temporary.stat().st_size < MIN_BYTES
    ):
        temporary.unlink(missing_ok=True)

        raise RuntimeError(
            "Temporary parallax video is invalid."
        )

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
        converted.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        raise

    print("DEPTH PARALLAX SUCCESS")
    print("Version:", VERSION)
    print("Movement profile:", profile["name"])
    print("Strength:", args.strength)
    print("Output:", output_path)
    print("Frames:", args.frames)
    print("FPS:", args.fps)
    print(
        "Resolution:",
        f"{args.width}x{args.height}",
    )
    print(
        "Duration:",
        f"{args.frames / args.fps:.3f}s",
    )
    print(
        "Size bytes:",
        output_path.stat().st_size,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "DEPTH PARALLAX CANCELLED",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as exc:
        print(
            f"DEPTH PARALLAX FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)