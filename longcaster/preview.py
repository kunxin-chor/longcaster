from __future__ import annotations

import os
from pathlib import Path
import subprocess
import uuid
from typing import Any

from .project import ProjectError, ProjectStore, sha256_file
from .timeline_export import _ffmpeg, _finish_process, _write_video_frames


def encode_project_preview(
    *, store: ProjectStore, card: dict[str, Any], images: Any,
    preset: str = "p4", cq: int = 17,
) -> tuple[dict[str, Any], Path]:
    """Encode a silent project preview used for review and visual frame selection."""
    if not hasattr(images, "shape") or len(images.shape) != 4 or int(images.shape[0]) < 1:
        raise ProjectError("project preview requires at least one decoded IMAGE frame")
    artifact_hash = card.get("artifact_sha256")
    if not artifact_hash:
        raise ProjectError("project preview source card has no artifact hash")
    height, width = int(images.shape[1]), int(images.shape[2])
    destination = store.absolute_path(
        f"previews/{card['id']}/card_{int(card['artifact_number']):04d}_{artifact_hash[:12]}.mp4"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp.mp4")
    process: subprocess.Popen | None = None
    try:
        command = [
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s:v", f"{width}x{height}",
            "-r", "24", "-i", "pipe:0", "-an", "-c:v", "h264_nvenc",
            "-preset", preset, "-tune", "hq", "-rc", "vbr", "-cq", str(int(cq)),
            "-b:v", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        _write_video_frames(process, images)
        _finish_process(process, "NVENC project preview encode")
        process = None
        os.replace(temporary, destination)
        manifest = store.record_preview(
            card_id=card["id"],
            preview_path=destination,
            preview_sha256=sha256_file(destination),
            source_artifact_sha256=artifact_hash,
        )
        return manifest, destination
    finally:
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                pass
        temporary.unlink(missing_ok=True)
