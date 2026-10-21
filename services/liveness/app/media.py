"""Safe handling of uploaded images and videos: size limits, type checks, bounded decoding, evenly spaced frame sampling."""
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", "40000000")  # must be set before cv2 is imported (decompression-bomb guard)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv"}


def check_ext(f: UploadFile, allowed: set[str], what: str) -> str:
    ext = Path(f.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(415, f"Unsupported {what} type '{ext or 'none'}'. Allowed: {', '.join(sorted(allowed))}.")
    return ext


def read_limited(f: UploadFile, limit_mb: int) -> bytes:
    limit, chunks, total = limit_mb * 1024 * 1024, [], 0
    while chunk := f.file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"{f.filename}: larger than {limit_mb} MB.")
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(422, f"{f.filename}: empty file.")
    return b"".join(chunks)


def decode_image(f: UploadFile, limit_mb: int) -> np.ndarray:
    check_ext(f, IMAGE_EXT, "image")
    data = read_limited(f, limit_mb)
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        img = None
    if img is None or img.size == 0:
        raise HTTPException(422, f"{f.filename}: not a readable image (or it is too large to decode safely).")
    return img


@contextmanager
def saved_video(f: UploadFile, limit_mb: int) -> Iterator[str]:
    """Stream an uploaded video to a temporary file (size-capped); it is deleted when the block ends."""
    ext = check_ext(f, VIDEO_EXT, "video")
    limit, written = limit_mb * 1024 * 1024, 0
    with tempfile.TemporaryDirectory(prefix="lv-video-") as tmp:
        path = Path(tmp) / f"video{ext}"
        with path.open("wb") as out:
            while chunk := f.file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"{f.filename}: larger than {limit_mb} MB.")
                out.write(chunk)
        if written == 0:
            raise HTTPException(422, f"{f.filename}: empty file.")
        yield str(path)


def sample_frames(path: str, n: int) -> Iterator[tuple[int, float | None, np.ndarray]]:
    """Yield (frame index, seconds, BGR frame) for up to n evenly spaced frames. Raises 422 if the video cannot be read."""
    cap = cv2.VideoCapture(path)
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not cap.isOpened() or total <= 0:
            raise HTTPException(422, "The video could not be decoded.")
        idxs = sorted({int(round(i)) for i in np.linspace(0, total - 1, min(n, total))})
        got = 0
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if ok and frame is not None:
                got += 1
                yield idx, (round(idx / fps, 2) if fps else None), frame
        if got == 0:
            raise HTTPException(422, "No frame of the video could be decoded.")
    finally:
        cap.release()
