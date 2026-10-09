#!/usr/bin/env python3
"""Create a validated depth-guided 2.5D parallax video."""

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


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

    if not math.isfinite(number) or not 0.0 <= number <= 0.04:
        raise argparse.ArgumentTypeError(
            "Must be between 0 and 0.04."
        )
    return number


def validate_video(path, expected_width, expected_height, expected_frames, fps):
    if not path.is_file() or path.stat().st_size < 1000:
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
                f"Wrong video dimensions: {width}x{height}"
            )

        if frame_count < expected_frames:
            raise RuntimeError(
                f"Video has only {frame_count} frames; "
                f"expected {expected_frames}."
            )

        if not math.isfinite(actual_fps) or actual_fps <= 0:
            raise RuntimeError("Video FPS could not be validated.")

        if abs(actual_fps - fps) > 1.0:
            raise RuntimeError(
                f"Unexpected video FPS: {actual_fps}; expected {fps}."
            )
    finally:
        cap.release()


def main():
    parser = argparse.ArgumentParser(
        description="Render a depth-guided 2.5D parallax clip."
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

    if args.fps > 60:
        parser.error("--fps must not exceed 60.")
    if args.width < 64 or args.height < 64:
        parser.error("--width and --height must be at least 64.")
    if args.frames > args.fps * 600:
        parser.error("--frames cannot exceed ten minutes of video.")

    image_path = Path(args.image).resolve()
    depth_path = Path(args.depth).resolve()
    output_path = Path(args.output).resolve()

    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if not depth_path.is_file():
        raise FileNotFoundError(f"Depth map not found: {depth_path}")

    if output_path in (image_path, depth_path):
        raise RuntimeError("Output path must differ from input files.")

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

    target_ratio = args.width / args.height
    source_ratio = image.shape[1] / image.shape[0]

    if source_ratio > target_ratio:
        new_height = args.height
        new_width = max(
            args.width,
            round(image.shape[1] * new_height / image.shape[0]),
        )
    else:
        new_width = args.width
        new_height = max(
            args.height,
            round(image.shape[0] * new_width / image.shape[1]),
        )

    image = cv2.resize(
        image,
        (new_width, new_height),
        interpolation=cv2.INTER_LANCZOS4,
    )
    depth = cv2.resize(
        depth,
        (new_width, new_height),
        interpolation=cv2.INTER_CUBIC,
    )

    left = (new_width - args.width) // 2
    top = (new_height - args.height) // 2

    image = image[
        top:top + args.height,
        left:left + args.width,
    ]
    depth = depth[
        top:top + args.height,
        left:left + args.width,
    ]

    depth = cv2.GaussianBlur(depth, (0, 0), 3.0)
    depth_float = depth.astype(np.float32) / 255.0

    height, width = depth.shape
    base_x, base_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )

    # Small overscan helps prevent empty borders during movement.
    zoom = 1.035
    zoom_x = (width - width / zoom) / 2.0
    zoom_y = (height - height / zoom) / 2.0

    map_x_base = (base_x - zoom_x) / zoom
    map_y_base = (base_y - zoom_y) / zoom

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        output_path.stem + ".temporary.mp4"
    )

    if temporary in (image_path, depth_path):
        raise RuntimeError("Temporary output conflicts with an input file.")

    temporary.unlink(missing_ok=True)

    writer = cv2.VideoWriter(
        str(temporary),
        cv2.VideoWriter_fourcc(*"mp4v"),
        args.fps,
        (width, height),
    )

    if not writer.isOpened():
        writer.release()
        raise RuntimeError("Could not initialize MP4 video writer.")

    displacement = (depth_float - 0.5) * 2.0

    try:
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
                    f"Invalid rendered frame: {frame_index + 1}"
                )

            writer.write(frame)

            if (frame_index + 1) % args.fps == 0:
                print(
                    f"Rendered {frame_index + 1}/{args.frames} frames",
                    flush=True,
                )
    finally:
        writer.release()

    if not temporary.is_file() or temporary.stat().st_size < 1000:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Temporary parallax video is invalid.")

    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg:
        converted = output_path.with_name(
            output_path.stem + ".converted.mp4"
        )
        converted.unlink(missing_ok=True)

        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(temporary),
                "-an",
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-r", str(args.fps),
                "-movflags", "+faststart",
                str(converted),
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            converted.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                "FFmpeg H.264 conversion failed: "
                + result.stderr[-2000:]
            )

        if not converted.is_file() or converted.stat().st_size < 1000:
            converted.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Converted MP4 is invalid.")

        converted.replace(output_path)
        temporary.unlink(missing_ok=True)
    else:
        # Keep the OpenCV MP4 if FFmpeg is not installed.
        temporary.replace(output_path)

    validate_video(
        output_path,
        args.width,
        args.height,
        args.frames,
        args.fps,
    )

    print("DEPTH PARALLAX SUCCESS")
    print("Output:", output_path)
    print("Frames:", args.frames)
    print("FPS:", args.fps)
    print("Resolution:", f"{args.width}x{args.height}")
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