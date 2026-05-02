from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Sequence


class ToolError(RuntimeError):
    pass


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolError(f"Required tool not found: {name}")
    return path


def run_command(args: Sequence[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise ToolError(f"Command failed: {' '.join(args)}\n{result.stderr.strip()}")
    return result


def run_json(args: Sequence[str], timeout: int | None = None) -> dict:
    result = run_command(args, timeout=timeout)
    return json.loads(result.stdout)


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def format_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    s = total % 60
    m = (total // 60) % 60
    h = total // 3600
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def read_wav_energy(path: Path, window_seconds: float = 0.25) -> list[tuple[float, float]]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frame_count = wf.getnframes()
        window_frames = max(1, int(sample_rate * window_seconds))
        energies: list[tuple[float, float]] = []
        pos = 0
        max_possible = float(2 ** (8 * sample_width - 1))
        while pos < frame_count:
            raw = wf.readframes(window_frames)
            if not raw:
                break
            count = len(raw) // sample_width
            if count == 0:
                break
            total = 0.0
            samples = 0
            for offset in range(0, len(raw), sample_width * channels):
                for ch in range(channels):
                    start = offset + ch * sample_width
                    chunk = raw[start : start + sample_width]
                    if len(chunk) != sample_width:
                        continue
                    value = int.from_bytes(chunk, "little", signed=True)
                    total += abs(value) / max_possible
                    samples += 1
            energies.append((pos / sample_rate, total / max(samples, 1)))
            pos += window_frames
    return energies
