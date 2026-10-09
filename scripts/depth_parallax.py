#!/usr/bin/env python3
"""Create a depth-guided 2.5D parallax video from an image and depth map."""

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero.")
    return number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--depth", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--width", type=positive_int, default=720)
    parser.add_argument("--height", type=positive_int, default=1280)
    parser.add_argument("--fps", type=positive_int, default=24)
    parser.add_argument("--frames", type=positive_int, default=144)
    parser.add_argument("--strength", type=float, default=0.018)
    args = parser.parse_args()

    if not 0.0 <= args.strength <= 0.04:
        raise ValueError("--strength must be between 0 and 0.04.")

    image_path = Path(args.image).resolve()
    depth_path = Path(args.depth).resolve()
    output_path = Path(args.output).resolve()

    if not image_path.is_file() or not depth_path.is_file():
        raise FileNotFoundError("Image or depth-map file is missing.")

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    depth = cv2.imread(str(depth_path), cv2.IMREAD_GRAYSCALE)

    if image is None or depth is None:
        raise RuntimeError("Could not decode image or depth map.")

    if image.shape[0] < 64 or image.shape[1] < 64:
        raise RuntimeError("Source image is too small.")

    # Fit image to the target aspect ratio, then crop the center.
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
        image, (new_width, new_height),
        interpolation=cv2.INTER_LANCZOS4,
    )
    depth = cv2.resize(
        depth, (new_width, new_height),
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

    # Smooth depth noise to reduce jitter and harsh displacement edges.
    depth = cv2.GaussianBlur(depth, (0, 0), 3.0)
    depth_float = depth.astype(np.float32) / 255.0

    height, width = depth.shape
    base_x, base_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )

    # Slight overscan reduces exposed edges during movement.
    zoom = 1.035
    zoom_x = (width - width / zoom) / 2.0
    zoom_y = (height - height / zoom) / 2.0

    map_x_base = (base_x - zoom_x) / zoom
    map_y_base = (base_y - zoom_y) / zoom

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        output_path.stem + ".temporary.mp4"
    )

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(temporary),
        fourcc,
        args.fps,
        (width, height),
    )

    if not writer.isOpened():
        raise RuntimeError("Could not initialize MP4 video writer.")

    try:
        for frame_index in range(args.frames):
            progress = frame_index / max(args.frames - 1, 1)

            # Smooth camera translation, with a restrained return arc.
            phase = 2.0 * math.pi * progress
            shift_x = args.strength * width * math.sin(phase)
            shift_y = args.strength * height * (
                math.cos(phase) - 1.0
            ) / 2.0

            # Depth controls displacement per pixel. This is a 2.5D
            # warp, not a reconstruction of hidden surfaces.
            displacement = (depth_float - 0.5) * 2.0
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

            writer.write(frame)

            if (frame_index + 1) % max(args.fps, 1) == 0:
                print(
                    f"Rendered {frame_index + 1}/{args.frames} frames",
                    flush=True,
                )
    finally:
        writer.release()

    if not temporary.is_file() or temporary.stat().st_size < 1000:
        raise RuntimeError("Parallax video output is missing or too small.")

    # Re-encode to broadly compatible H.264 using FFmpeg when available.
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(temporary),
                "-an",
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "FFmpeg H.264 conversion failed: " + result.stderr[-2000:]
            )
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(output_path)

    if not output_path.is_file() or output_path.stat().st_size < 1000:
        raise RuntimeError("Final parallax clip is invalid.")

    print("DEPTH PARALLAX SUCCESS")
    print("Output:", output_path)
    print("Frames:", args.frames)
    print("FPS:", args.fps)
    print("Resolution:", f"{width}x{height}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"DEPTH PARALLAX FAILED: {exc}", file=sys.stderr)
        sys.exit(1)