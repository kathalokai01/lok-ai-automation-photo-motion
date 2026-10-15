#!/usr/bin/env python3
"""Validated depth-aligned 2.5D parallax renderer."""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

VERSION = "depth-parallax-v3"
MIN_BYTES = 1000
MAX_FPS = 60
MAX_SECONDS = 600
MIN_DIMENSION = 64
MAX_DIMENSION = 4096


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Must be a positive integer."
        ) from exc

    if number <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero.")

    return number


def bounded_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("Must be a number.") from exc

    if not math.isfinite(number) or not 0.0 <= number <= 0.04:
        raise argparse.ArgumentTypeError("Must be between 0 and 0.04.")

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
        raise RuntimeError("Output video is missing or too small.")

    cap = cv2.VideoCapture(str(path))

    try:
        if not cap.isOpened():
            raise RuntimeError("Cannot reopen generated video.")

        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        actual_fps = float(cap.get(cv2.CAP_PROP_FPS))

        if (actual_width, actual_height) != (width, height):
            raise RuntimeError(
                f"Wrong dimensions: {actual_width}x{actual_height}; "
                f"expected {width}x{height}."
            )

        # A truncated clip must not be accepted as a completed scene.
        if frame_count != expected_frames:
            raise RuntimeError(
                f"Frame count is {frame_count}; expected {expected_frames}."
            )

        if not math.isfinite(actual_fps) or actual_fps <= 0:
            raise RuntimeError("Video FPS could not be validated.")

        if abs(actual_fps - fps) > 1.0:
            raise RuntimeError(
                f"Unexpected FPS: {actual_fps}; expected {fps}."
            )

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok_first, first = cap.read()

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_count - 1)
        ok_last, last = cap.read()

        if not ok_first or first is None:
            raise RuntimeError("Could not decode first output frame.")

        if not ok_last or last is None:
            raise RuntimeError("Could not decode last output frame.")

    finally:
        cap.release()


def center_crop_parameters(source_width, source_height, target_width, target_height):
    """Return one crop transform shared by RGB and depth images."""
    source_ratio = source_width / source_height
    target_ratio = target_width / target_height

    if source_ratio > target_ratio:
        crop_height = source_height
        crop_width = max(1, round(source_height * target_ratio))
        left = (source_width - crop_width) // 2
        top = 0
    else:
        crop_width = source_width
        crop_height = max(1, round(source_width / target_ratio))
        left = 0
        top = (source_height - crop_height) // 2

    return left, top, crop_width, crop_height


def apply_shared_crop(image, depth, target_width, target_height):
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
        raise RuntimeError("Shared image/depth crop produced empty output.")

    image_out = cv2.resize(
        image_crop,
        (target_width, target_height),
        interpolation=cv2.INTER_LANCZOS4,
    )

    # Depth is a scalar field; avoid sharpening interpolation artifacts.
    depth_out = cv2.resize(
        depth_crop,
        (target_width, target_height),
        interpolation=cv2.INTER_LINEAR,
    )

    if image_out.shape[:2] != depth_out.shape[:2]:
        raise RuntimeError("Image/depth alignment failed.")

    return image_out, depth_out


def render_frames(image, depth, args, writer):
    height, width = image.shape[:2]

    depth = cv2.GaussianBlur(depth, (0, 0), 2.0)
    depth_float = depth.astype(np.float32) / 255.0
    displacement = (depth_float - 0.5) * 2.0

    base_x, base_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )

    zoom = 1.035
    zoom_x = (width - width / zoom) / 2.0
    zoom_y = (height - height / zoom) / 2.0

    map_x_base = (base_x - zoom_x) / zoom
    map_y_base = (base_y - zoom_y) / zoom

    for frame_index in range(args.frames):
        progress = frame_index / max(args.frames - 1, 1)
        phase = 2.0 * math.pi * progress

        shift_x = args.strength * width * math.sin(phase)
        shift_y = (
            args.strength * height
            * (math.cos(phase) - 1.0) / 2.0
        )

        map_x = (
            map_x_base + shift_x * displacement
        ).astype(np.float32)

        map_y = (
            map_y_base + shift_y * displacement
        ).astype(np.float32)

        frame = cv2.remap(
            image,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )

        if frame is None or frame.shape != image.shape:
            raise RuntimeError(
                f"Invalid frame generated at {frame_index + 1}."
            )

        writer.write(frame)

        if not writer.isOpened():
            raise RuntimeError("Video writer stopped unexpectedly.")

        if (frame_index + 1) % args.fps == 0:
            print(
                f"Rendered {frame_index + 1}/{args.frames} frames",
                flush=True,
            )


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

    if result.returncode:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            "FFmpeg H.264 conversion failed: "
            + result.stderr[-2000:]
        )

    if not destination.is_file() or destination.stat().st_size < MIN_BYTES:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Converted H.264 video is invalid.")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Render validated, depth-guided 2.5D parallax."
    )

    parser.add_argument("--image", required=True)
    parser.add_argument("--depth", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--width", type=positive_int, default=720)
    parser.add_argument("--height", type=positive_int, default=1280)
    parser.add_argument("--fps", type=positive_int, default=24)
    parser.add_argument("--frames", type=positive_int, default=144)
    parser.add_argument("--strength", type=bounded_float, default=0.018)

    args = parser.parse_args()

    if args.fps > MAX_FPS:
        parser.error(f"--fps must not exceed {MAX_FPS}.")

    try:
        validate_dimensions(args.width, args.height)
    except RuntimeError as exc:
        parser.error(str(exc))

    if args.frames > args.fps * MAX_SECONDS:
        parser.error("--frames cannot exceed ten minutes of video.")

    image_path = Path(args.image).resolve()
    depth_path = Path(args.depth).resolve()
    output_path = Path(args.output).resolve()

    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")

    if not depth_path.is_file():
        raise FileNotFoundError(f"Depth map not found: {depth_path}")

    if output_path in (image_path, depth_path):
        raise RuntimeError("Output must differ from both input files.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary = output_path.with_name(
        output_path.stem + ".temporary.mp4"
    )
    converted = output_path.with_name(
        output_path.stem + ".converted.mp4"
    )

    if temporary in (image_path, depth_path) or converted in (
        image_path, depth_path
    ):
        raise RuntimeError("Temporary output conflicts with an input file.")

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    depth = cv2.imread(str(depth_path), cv2.IMREAD_GRAYSCALE)

    if image is None:
        raise RuntimeError("Could not decode source image.")

    if depth is None:
        raise RuntimeError("Could not decode depth map.")

    if image.shape[0] < MIN_DIMENSION or image.shape[1] < MIN_DIMENSION:
        raise RuntimeError("Source image is too small.")

    if depth.shape[0] < 16 or depth.shape[1] < 16:
        raise RuntimeError("Depth map is too small.")

    image, depth = apply_shared_crop(
        image, depth, args.width, args.height
    )

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
        raise RuntimeError("Could not initialize MP4 video writer.")

    try:
        render_frames(image, depth, args, writer)
    except Exception:
        writer.release()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        writer.release()

    if not temporary.is_file() or temporary.stat().st_size < MIN_BYTES:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Temporary parallax video is invalid.")

    try:
        # First validate the OpenCV output before transcoding.
        validate_video(
            temporary,
            args.width,
            args.height,
            args.frames,
            args.fps,
        )

        if encode_h264(temporary, converted, args.fps, args.frames):
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
            # If FFmpeg is absent, retain the validated OpenCV MP4.
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
    print("Output:", output_path)
    print("Frames:", args.frames)
    print("FPS:", args.fps)
    print("Resolution:", f"{args.width}x{args.height}")
    print("Duration:", f"{args.frames / args.fps:.3f}s")
    print("Size bytes:", output_path.stat().st_size)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("DEPTH PARALLAX CANCELLED", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH PARALLAX FAILED: {exc}", file=sys.stderr)
        sys.exit(1)