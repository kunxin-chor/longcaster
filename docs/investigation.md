# Implementation verification notes

This file records the source checks used while implementing the MVP. The full reasoning remains in `LONGCASTER_ARCHITECTURE.md`.

- Installed PDD source exposes a patched `MODEL` plus its trained `SIGMAS`. LongCaster forwards both through Comfy's standard Basic Guider / advanced sampler path and does not recreate the patch or schedule. Its own recipe text says PDD on a hybrid-merged trunk is untested, so stock REF2VA + PDD is the supported baseline and hybrid + PDD remains an explicit experiment.
- Native `MiniMaxH3ReferenceToVideo.execute` accepts CLIP, video/audio VAEs, prompt/canvas/frame count, and dynamically indexed image/video/video-audio/audio dictionaries. LongCaster materializes those dictionaries from `resolve_reference_set` so native reference order and bindings remain authoritative.
- `mmh3_media.build_h3_continuation_handover` creates a fresh joint AV target with copied source tails and nested masks using `0=preserve, 1=denoise`. The MVP uses 39 pixel frames for both video and audio handover.
- Current Comfy MiniMax H3 model code handles video and audio denoise masks separately and its sampler restores the protected latent region after each denoise call.
- MMH3 `pack_h3_result(... latent_origin="sampler_output")`, `save_archive`, `load_archive(verify="full")`, latent contract validation, and resource materialization are used directly.
- Current MMH3 exports `pack_h3_result` from `mmh3_media.public_api` rather than the package root. The adapter normalizes that public split during discovery.
- The local `ComfyUI_MinimaxH3HybridLoader` produces a normal Comfy `MODEL`; no special LongCaster integration is required. Its output can feed LongCaster directly for the standard reference test. It can technically replace the PDD example's UNET input, but current PDD source does not claim that merged trunk is compatible.

The cross-process smoke test found and fixed the MMH3 public API split before UI work was finalized.
