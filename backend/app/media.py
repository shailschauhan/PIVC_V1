"""Browser-playable preview copies.

Uploaded PIVC videos are often MPEG-4 Part 2 / HEVC / other codecs that Chrome and Edge cannot play.
The original file is never modified (it is what gets analysed); reviewers watch an H.264 + AAC
copy with the index at the front (`faststart`) so seeking works over range requests.
"""
import logging
import subprocess
import threading
from pathlib import Path

import cv2

log = logging.getLogger(__name__)
BROWSER_CODECS = {"avc1", "h264", "x264", "vp09", "vp08", "av01"}
_lock = threading.Lock()
_inflight: dict[str, threading.Lock] = {}


def _fourcc(path: Path) -> str:
    cap = cv2.VideoCapture(str(path))
    f = int(cap.get(cv2.CAP_PROP_FOURCC))
    cap.release()
    return "".join(chr((f >> 8 * i) & 0xFF) for i in range(4)).lower()


def preview_path(video: Path) -> Path:
    return video.with_name("preview.mp4")


def ensure_preview(video_path: str) -> Path:
    """Return a file the browser can play: the original if its codec is fine, else a transcoded copy."""
    video = Path(video_path)
    preview = preview_path(video)
    if preview.is_file():
        return preview
    if video.suffix.lower() in (".mp4", ".webm") and _fourcc(video) in BROWSER_CODECS:
        return video
    with _lock:
        guard = _inflight.setdefault(str(video), threading.Lock())
    with guard:  # concurrent viewers wait for one transcode instead of starting several
        if preview.is_file():
            return preview
        import imageio_ffmpeg
        tmp = preview.with_name("preview.part.mp4")
        base = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(video),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        # videos without an audio track fail with -c:a, so try with audio first and fall back to video-only
        for audio in (["-c:a", "aac", "-b:a", "96k"], ["-an"]):
            proc = subprocess.run([*base, *audio, str(tmp)], capture_output=True, text=True, timeout=1800)
            if proc.returncode == 0:
                break
            tmp.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[-300:]}")
        tmp.replace(preview)
        log.info("created preview for %s", video)
    return preview
