from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any

import folder_paths

from .mmh3_adapter import load_packet, primary_latent
from .project import ProjectError, ProjectStore, sha256_file
from .timeline import retained_frame_span, selected_cards


FPS = 24.0
_NVENC_PROBES: dict[str, bool] = {}


def _ffmpeg() -> str:
    candidates: list[str] = []
    forced = os.environ.get("VHS_FORCE_FFMPEG_PATH")
    if forced:
        candidates.append(forced)
    try:
        import imageio_ffmpeg

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate:
            candidates.append(str(candidate))
    except Exception:
        pass
    system = shutil.which("ffmpeg")
    if system and system not in candidates:
        candidates.append(system)
    for local_name in ("ffmpeg.exe", "ffmpeg"):
        local = str((Path.cwd() / local_name).resolve())
        if Path(local).is_file() and local not in candidates:
            candidates.append(local)
    for candidate in candidates:
        if _has_nvenc(candidate):
            return candidate
    raise ProjectError(
        "No working h264_nvenc encoder was found. Install an NVENC-enabled ffmpeg "
        "and a compatible NVIDIA driver."
    )


def _has_nvenc(executable: str) -> bool:
    key = str(Path(executable).resolve())
    if key in _NVENC_PROBES:
        return _NVENC_PROBES[key]
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:r=1",
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        available = result.returncode == 0
    except Exception:
        available = False
    _NVENC_PROBES[key] = available
    return available


def _card_latent(store: ProjectStore, card: dict[str, Any]) -> dict[str, Any]:
    relative = card.get("master_path") if card["status"] == "ACCEPTED" else card.get("draft_path")
    if not relative:
        raise ProjectError(f"card {card['artifact_number']} has no archive")
    path = store.absolute_path(relative)
    if not path.is_file() or sha256_file(path) != card.get("artifact_sha256"):
        raise ProjectError(f"card {card['artifact_number']} archive is missing or corrupt")
    packet = load_packet(path, verify="on_access")
    latent, _ = primary_latent(packet)
    return latent


def _write_video_frames(process: subprocess.Popen, images: Any) -> None:
    import comfy.model_management

    if process.stdin is None:
        raise ProjectError("NVENC input pipe was not created")
    for frame in images:
        comfy.model_management.throw_exception_if_processing_interrupted()
        data = (frame[..., :3].float() * 255.0).clamp(0, 255).byte().cpu().contiguous().numpy()
        try:
            process.stdin.write(data.tobytes())
        except BrokenPipeError as exc:
            detail = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise ProjectError(f"NVENC stopped while receiving frames: {detail}") from exc


def _finish_process(process: subprocess.Popen, label: str) -> None:
    if process.stdin is not None:
        process.stdin.close()
    detail = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    return_code = process.wait()
    if return_code != 0:
        raise ProjectError(f"{label} failed ({return_code}): {detail[-4000:]}")


def export_timeline_nvenc(
    *,
    projects_root: str | Path,
    project_name: str,
    video_vae: Any,
    audio_vae: Any,
    filename_prefix: str,
    include_active_draft: bool,
    cq: int,
    preset: str,
    audio_bitrate: str,
) -> tuple[Path, str, int, int]:
    import torch
    import torch.nn.functional as functional
    from comfy_extras.nodes_audio import vae_decode_audio

    store = ProjectStore(projects_root, project_name)
    manifest = store.load()
    cards = selected_cards(manifest, include_active_draft)
    if not cards:
        raise ProjectError("Timeline export requires at least one accepted card or an included active draft")

    executable = _ffmpeg()
    output_root = Path(folder_paths.get_output_directory()).resolve()
    video_process: subprocess.Popen | None = None
    temporary_video: Path | None = None
    temporary_audio: Path | None = None
    temporary_mux: Path | None = None
    audio_handle = None
    final_path: Path | None = None
    subfolder = ""
    total_frames = 0
    sample_rate: int | None = None
    channels: int | None = None

    try:
        for card in cards:
            latent = _card_latent(store, card)
            parts = latent["samples"].unbind()
            if len(parts) != 2:
                raise ProjectError(f"card {card['artifact_number']} is not a joint H3 video/audio latent")
            video_latent, audio_latent = parts
            images = video_vae.decode(video_latent)
            if len(images.shape) == 5:
                images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
            try:
                context, end = retained_frame_span(card, int(images.shape[0]))
            except ValueError as exc:
                raise ProjectError(f"card {card['artifact_number']} {exc}") from exc
            images = images[context:end]
            retained_frames = int(images.shape[0])

            if video_process is None:
                height, width = int(images.shape[1]), int(images.shape[2])
                folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                    filename_prefix, str(output_root), width, height
                )
                final_path = Path(folder) / f"{filename}_{counter:05}_.mp4"
                token = uuid.uuid4().hex
                temporary_video = final_path.with_name(f".{final_path.stem}.{token}.video.mp4")
                temporary_audio = final_path.with_name(f".{final_path.stem}.{token}.audio.f32le")
                temporary_mux = final_path.with_name(f".{final_path.stem}.{token}.mux.mp4")
                audio_handle = temporary_audio.open("xb")
                command = [
                    executable, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s:v", f"{width}x{height}", "-r", "24", "-i", "pipe:0", "-an",
                    "-c:v", "h264_nvenc", "-preset", preset, "-tune", "hq",
                    "-rc", "vbr", "-cq", str(int(cq)), "-b:v", "0",
                    "-pix_fmt", "yuv420p", str(temporary_video),
                ]
                video_process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )

            _write_video_frames(video_process, images)
            total_frames += retained_frames

            audio = vae_decode_audio(audio_vae, {"samples": audio_latent})
            current_rate = int(audio["sample_rate"])
            waveform = audio["waveform"]
            current_channels = int(waveform.shape[1])
            if sample_rate is None:
                sample_rate, channels = current_rate, current_channels
            elif current_rate != sample_rate or current_channels != channels:
                raise ProjectError("audio sample rate or channel count changed between cards")
            trim_samples = round(context * current_rate / FPS)
            wanted_samples = round(retained_frames * current_rate / FPS)
            waveform = waveform[..., trim_samples:trim_samples + wanted_samples]
            if int(waveform.shape[-1]) < wanted_samples:
                waveform = functional.pad(waveform, (0, wanted_samples - int(waveform.shape[-1])))
            waveform = waveform[..., :wanted_samples]
            pcm = waveform[0].transpose(0, 1).float().cpu().contiguous().numpy().astype("<f4", copy=False)
            audio_handle.write(pcm.tobytes())
            del latent, video_latent, audio_latent, images, audio, waveform, pcm

        if video_process is None or final_path is None or temporary_video is None:
            raise ProjectError("timeline contained no video frames")
        _finish_process(video_process, "NVENC timeline encode")
        video_process = None
        audio_handle.flush()
        os.fsync(audio_handle.fileno())
        audio_handle.close()
        audio_handle = None

        if sample_rate is None or channels is None or temporary_audio is None or temporary_mux is None:
            raise ProjectError("timeline audio was not decoded")
        mux = [
            executable, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(temporary_video),
            "-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels),
            "-i", str(temporary_audio),
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-shortest", "-movflags", "+faststart", str(temporary_mux),
        ]
        result = subprocess.run(mux, capture_output=True)
        if result.returncode != 0:
            raise ProjectError(
                f"timeline audio mux failed ({result.returncode}): "
                + result.stderr.decode("utf-8", "replace")[-4000:]
            )
        os.replace(temporary_mux, final_path)
        return final_path, str(subfolder).replace("\\", "/"), total_frames, len(cards)
    finally:
        if video_process is not None:
            try:
                if video_process.stdin:
                    video_process.stdin.close()
                video_process.terminate()
                video_process.wait(timeout=5)
            except Exception:
                pass
        if audio_handle is not None:
            audio_handle.close()
        for path in (temporary_video, temporary_audio, temporary_mux):
            if path is not None:
                path.unlink(missing_ok=True)
