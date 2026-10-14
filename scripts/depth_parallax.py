
#!/usr/bin/env python3
"""Validated, resumable-compatible depth-guided 2.5D parallax renderer."""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


MIN_BYTES = 1000
MAX_FPS = 60
MAX_SECONDS = 600


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
        raise argparse.ArgumentTypeError(
            "Must be between 0 and 0.04."
        )

    return number


def validate_video(path, expected_width, expected_height, expected_frames, fps):
    path = Path(path)

    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise RuntimeError("Output video is missing or too small.")

    cap = cv2.VideoCapture(str(path))

    try:
        if not cap.isOpened():
            raise RuntimeError("Cannot reopen generated video.")

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        actual_fps = float(cap.get(cv2.CAP_PROP_FPS))

        if (width, height) != (expected_width, expected_height):
            raise RuntimeError(
                f"Wrong dimensions: {width}x{height}; "
                f"expected {expected_width}x{expected_height}."
            )

        # Some containers report one frame less than expected; never accept
        # a materially incomplete render.
        if frame_count < expected_frames:
            raise RuntimeError(
                f"Video has {frame_count} frames; "
                f"expected {expected_frames}."
            )

        if frame_count > expected_frames + max(2, round(fps * 0.1)):
            raise RuntimeError(
                f"Video has unexpected extra frames: {frame_count}."
            )

        if not math.isfinite(actual_fps) or actual_fps <= 0:
            raise RuntimeError("Video FPS could not be validated.")

        if abs(actual_fps - fps) > 1.0:
            raise RuntimeError(
                f"Unexpected FPS: {actual_fps}; expected {fps}."
            )

        # Decode the first and last frames so a corrupt container is not
        # accepted merely because its header reports plausible metadata.
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok_first, first = cap.read()

        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_count - 1))
        ok_last, last = cap.read()

        if not ok_first or first is None:
            raise RuntimeError("Could not decode the first output frame.")

        if not ok_last or last is None:
            raise RuntimeError("Could not decode the last output frame.")

    finally:
        cap.release()


def encode_h264(source, destination, fps):
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


def center_crop(image, width, height):
    source_height, source_width = image.shape[:2]
    target_ratio = width / height
    source_ratio = source_width / source_height

    if source_ratio > target_ratio:
        new_height = height
        new_width = max(
            width,
            round(source_width * height / source_height),
        )
    else:
        new_width = width
        new_height = max(
            height,
            round(source_height * width / source_width),
        )

    resized = cv2.resize(
        image,
        (new_width, new_height),
        interpolation=cv2.INTER_LANCZOS4,
    )

    left = (new_width - width) // 2
    top = (new_height - height) // 2

    return resized[top:top + height, left:left + width]


def render_frames(image, depth, args, writer):
    height, width = image.shape[:2]

    # Mild blur avoids noisy depth edges producing distracting warping.
    depth = cv2.GaussianBlur(depth, (0, 0), 2.0)
    depth_float = depth.astype(np.float32) / 255.0
    displacement = (depth_float - 0.5) * 2.0

    base_x, base_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )

    # Overscan keeps movement from exposing empty borders.
    zoom = 1.035
    zoom_x = (width - width / zoom) / 2.0
    zoom_y = (height - height / zoom) / 2.0

    map_x_base = (base_x - zoom_x) / zoom
    map_y_base = (base_y - zoom_y) / zoom

    for frame_index in range(args.frames):
        progress = frame_index / max(args.frames - 1, 1)
        phase = 2.0 * math.pi * progress

        # Subtle cyclical camera drift. Depth determines each pixel's
        # displacement, creating a restrained 2.5D parallax effect.
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

        if (frame_index + 1) % args.fps == 0:
            print(
                f"Rendered {frame_index + 1}/{args.frames} frames",
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(
        description="Render a validated depth-guided 2.5D parallax clip."
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

    if args.width < 64 or args.height < 64:
        parser.error("--width and --height must be at least 64.")

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

    # Temporary files live beside the destination so final replacement
    # stays on the same filesystem and is atomic.
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

    if image.shape[0] < 64 or image.shape[1] < 64:
        raise RuntimeError("Source image is too small.")

    if depth.shape[0] < 16 or depth.shape[1] < 16:
        raise RuntimeError("Depth map is too small.")

    image = center_crop(image, args.width, args.height)
    depth = center_crop(depth, args.width, args.height)

    if image.shape[:2] != depth.shape[:2]:
        raise RuntimeError("Image and depth dimensions do not match.")

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
        if encode_h264(temporary, converted, args.fps):
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
            # Retain the OpenCV MP4 only if FFmpeg is unavailable.
            validate_video(
                temporary,
                args.width,
                args.height,
                args.frames,
                args.fps,
            )
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
