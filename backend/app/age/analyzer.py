"""Age estimation from face video with MiVOLO (https://github.com/WildChlamydia/MiVOLO).

MiVOLO pins timm==0.8.13.dev0 and ultralytics==8.1.0 (AGPL-3.0), which conflict with the main app's dependencies, so it
runs in a separate virtualenv as a long-lived worker process (backend/age_worker/worker.py) that this module starts
lazily, talks to over stdin/stdout, and restarts if it dies or times out. Only one video is analysed at a time.

Age is estimated per sampled frame for the largest face (using the matched body crop when visible). We aggregate with the
median, which is robust to a few bad frames, and report the spread so a rule can refuse to trust a wobbly estimate.
Published accuracy is about +/-3.7-4.2 years (mean absolute error) on benchmark photos; real video is usually worse.
"""
import json
import logging
import os
import queue
import statistics
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
BACKEND = Path(__file__).resolve().parents[2]
MAX_FRAME_ROWS = 64


class AgeUnavailable(RuntimeError):
    pass


def summarize(rows: list[dict]) -> dict[str, Any]:
    """Per-frame rows from the worker -> one estimate with its uncertainty. Pure function (unit-tested)."""
    aged = [r for r in rows if r.get("age") is not None]
    ages = sorted(r["age"] for r in aged)
    multi = sum(1 for r in rows if r.get("n_faces", 0) > 1)
    out: dict[str, Any] = {
        "frames_sampled": len(rows), "frames_with_face": sum(1 for r in rows if r.get("n_faces", 0) > 0),
        "frames_aged": len(aged), "multi_face_fraction": round(multi / len(rows), 3) if rows else 0.0,
        "frame_results": [{k: v for k, v in r.items()} for r in rows][:MAX_FRAME_ROWS],
    }
    if not ages:
        return {**out, "age": None, "age_low": None, "age_high": None, "spread": None}

    def pct(p: float) -> float:
        return ages[min(len(ages) - 1, int(round(p * (len(ages) - 1))))]

    out.update(age=round(statistics.median(ages), 1), age_low=round(pct(0.1), 1), age_high=round(pct(0.9), 1),
               spread=round(statistics.pstdev(ages), 2) if len(ages) > 1 else 0.0)
    return out


class AgeAnalyzer:
    def __init__(self, python: str, models_dir: str, frames: int, device: str = "cpu", load_timeout: int = 300, run_timeout: int = 600):
        self.python = Path(python)
        self.worker = BACKEND / "age_worker" / "worker.py"
        self.models_dir = Path(models_dir)
        self.frames, self.device = frames, device
        self.load_timeout, self.run_timeout = load_timeout, run_timeout
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._n = 0

    def missing(self) -> list[str]:
        need = [self.python, self.worker, self.models_dir / "yolov8x_person_face.pt", self.models_dir / "mivolo_v2" / "model.safetensors"]
        return [str(p) for p in need if not Path(p).exists()]

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            self._lines.put(line)
        self._lines.put(None)  # EOF: the process ended

    def _start(self) -> None:
        if self.missing():
            raise AgeUnavailable(f"Age model files not found: {', '.join(self.missing())}")
        log_path = BACKEND / "age_worker.log"
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            [str(self.python), "-u", str(self.worker), str(self.models_dir), self.device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_path.open("ab"), text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()
        msg = self._read(self.load_timeout, "starting the age model")
        if not msg.get("ready"):
            self._kill()
            raise AgeUnavailable(f"Age worker did not start (see {log_path.name})")
        log.info("age worker started")

    def _read(self, timeout: int, what: str) -> dict:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            raise AgeUnavailable(f"Timed out while {what}") from None
        if line is None:
            self._kill()
            raise AgeUnavailable(f"Age worker stopped unexpectedly while {what} (see age_worker.log)")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return self._read(timeout, what)  # ignore stray non-protocol output

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._kill()

    def analyze(self, video_path: str) -> dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            self._n += 1
            try:
                self._proc.stdin.write(json.dumps({"id": self._n, "video": str(video_path), "frames": self.frames}) + "\n")  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
            except OSError as e:
                self._kill()
                raise AgeUnavailable(f"Age worker is not responding: {e}") from e
            resp = self._read(self.run_timeout, "estimating age")
        if not resp.get("ok"):
            raise ValueError(resp.get("error", "age estimation failed"))
        return {"model": "MiVOLO v2", **summarize(resp["result"]["frames"])}


_analyzer: AgeAnalyzer | None = None


def get_analyzer() -> AgeAnalyzer:
    global _analyzer
    if _analyzer is None:
        from ..config import settings
        py = settings.age_python or str(BACKEND / ".venv-age" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
        _analyzer = AgeAnalyzer(py, settings.age_models_dir, settings.age_frames)
    return _analyzer


def shutdown() -> None:
    if _analyzer:
        _analyzer.stop()


def engine_status() -> dict:
    from ..config import settings
    if not settings.age_enabled:
        return {"engine": "mivolo", "ready": False, "reason": "disabled by configuration"}
    miss = get_analyzer().missing()
    return {"engine": "mivolo", "ready": not miss, "model": "MiVOLO v2",
            "reason": f"not found: {', '.join(miss)}" if miss else "ok"}
