"""Non-GPU MMH3 save/restart/direct-continuation smoke test.

Run from the repository root with:
  python tests/mmh3_backend_smoke.py --mmh3-root PATH_TO_ComfyUI_mmh3_media
"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))


def _configure(root: str) -> None:
    os.environ["LONGCASTER_MMH3_ROOT"] = str(Path(root).resolve())
    try:
        import folder_paths

        cache_root = Path(tempfile.gettempdir()) / "longcaster_comfy_temp"
        cache_root.mkdir(parents=True, exist_ok=True)
        folder_paths.set_temp_directory(str(cache_root))
    except ImportError:
        pass


def _write_source(root: str, output: Path) -> None:
    _configure(root)
    import torch
    from longcaster.mmh3_adapter import mmh3_api, pack_and_save_card

    api = mmh3_api()
    h3 = importlib.import_module(f"{api.__name__}.h3")
    frames = 56
    video_t = 17
    audio_t = round(frames * 40 / 24)
    latent = {
        "samples": h3.make_nested_tensor([
            torch.randn((1, 24, video_t, 4, 4)),
            torch.randn((1, 32, 2, audio_t)),
        ])
    }
    pack_and_save_card(
        path=output,
        latent=latent,
        card_metadata={
            "prompt": "synthetic source",
            "seed": 1,
            "generated_frame_count": frames,
            "context_frame_count": 0,
        },
        process_info={"contract": "longcaster_backend_smoke_v1"},
        mode="t2va",
        name="LongCaster backend smoke source",
    )


def _continue_after_restart(root: str, source: Path, output: Path) -> None:
    _configure(root)
    from longcaster.mmh3_adapter import load_packet, pack_and_save_card, prepare_continuation

    packet = load_packet(source, verify="full")
    target, plan = prepare_continuation(
        packet,
        target_frames=73,
        width=64,
        height=64,
        context_frames=39,
    )
    masks = target["noise_mask"].unbind()
    assert float(masks[0].min()) == 0.0 and float(masks[0].max()) == 1.0
    assert float(masks[1].min()) == 0.0 and float(masks[1].max()) == 1.0
    assert plan["video_handover_frames"] == 39
    pack_and_save_card(
        path=output,
        latent=target,
        card_metadata={
            "prompt": "synthetic continuation",
            "seed": 2,
            "generated_frame_count": 73,
            "context_frame_count": 39,
        },
        process_info={"contract": "longcaster_backend_smoke_v1", "continuation": plan},
        mode="t2va",
        name="LongCaster backend smoke continuation",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mmh3-root", required=True)
    parser.add_argument("--phase", choices=("write", "continue"))
    parser.add_argument("--source")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.phase == "write":
        _write_source(args.mmh3_root, Path(args.output))
        return
    if args.phase == "continue":
        _continue_after_restart(args.mmh3_root, Path(args.source), Path(args.output))
        return

    with tempfile.TemporaryDirectory(prefix="longcaster_mmh3_smoke_") as directory:
        source = Path(directory) / "card_0001.mmh3"
        continuation = Path(directory) / "card_0002.mmh3"
        common = [sys.executable, str(Path(__file__).resolve()), "--mmh3-root", args.mmh3_root]
        subprocess.run(common + ["--phase", "write", "--output", str(source)], check=True)
        subprocess.run(
            common + ["--phase", "continue", "--source", str(source), "--output", str(continuation)],
            check=True,
        )
        if not source.is_file() or not continuation.is_file():
            raise RuntimeError("backend smoke did not publish both MMH3 cards")
        print("PASS: MMH3 save -> process restart -> direct continuation -> save")


if __name__ == "__main__":
    main()
