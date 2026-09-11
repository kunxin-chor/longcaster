# MVP implementation notes

## PDD family pairing

PDD heads must match the diffusion trunk entering PDD Apply. Stock REF2VA uses the Ref2VA PDD file. Stock FL2VA and FL2VA with the separate reference patch use the FL2VA PDD file. The reference patch supplies conditioning support; it does not change the underlying trunk family. Keep PDD `partition_check` set to `error`, since allowing a mismatch can silently degrade the result.

## Implemented

- Patch-agnostic upstream `MODEL` input and Comfy Basic Guider sampling.
- Optional external `SIGMAS`, with finite/decreasing/terminal-zero validation and direct forwarding to the advanced sampler.
- PDD-first default (`require_external_sigmas=true`) and a supported stock-REF2VA PDD example workflow.
- Fixed REF2VA projects with MMH3 image/video/audio reference resolution on every card, including direct-continuation cards.
- Fixed T2VA projects as the standard sampling baseline.
- MMH3 card packing, full archive verification, immutable accepted masters, and hash validation.
- Direct joint audio/video latent continuation behind `DirectLatentContinuation`, with a 39-frame handover and no VAE round trip.
- Stage 2A automatic current-state anchors: lossless final-frame PNG extraction at acceptance, UUID-backed card metadata, native H3 `MiniMaxH3AddGuide` conditioning at the handover boundary, and optional minimal prompt reinforcement.
- Persistent UUID-based card state, ancestry, fingerprints, atomic manifests, locks, journals, restart reconciliation, and artifact diagnostics.
- Generate, Retry, Accept, Append, Resume, and a small separate decode/trim adapter.
- Streaming joined-timeline export through H.264 NVENC. It reads accepted cards in timeline order and removes each recorded continuation prefix before encoding, with optional active-draft inclusion for review.
- Minimal controller buttons and PDD REF2VA plus standard T2VA example workflows.
- Official model filenames appear only as editable example-workflow loader defaults; the Python node has no checkpoint names or model-loading path.
- Workflow seed serialization includes ComfyUI's separate `control after generate` value, keeping width, height, steps, scheduler, sampler, and later widgets aligned.
- The PDD workflow serializes optional PDD controls explicitly and sets `partition_check=error`; LongCaster also rejects a non-Euler sampler when its external-sigma requirement is enabled.
- Non-GPU unit tests and a real MMH3 cross-process backend smoke test.

## Priority-driven deviation

The initial MVP brief allowed T2VA Card 1 and deferred mixed card modes. The production note prioritizes REF2VA, the REF2VA/FL2VA hybrid model, reusable references, and PDD-ACC. LongCaster therefore supports a **fixed REF2VA project mode** immediately and sends the same reference packet into conditioning for every card. It still does not permit card-by-card mode switching, so the original mixed-mode deferral remains intact.

PDD remains external. LongCaster does not copy PDD logic; it consumes PDD Apply's patched model and exact schedule. The external-sigma requirement is enabled by default but can be disabled for the standard workflow.

The installed PDD implementation states that PDD on a hybrid-merged trunk is untested. LongCaster deliberately does not suppress that experiment or discard the patched model, but the supported first gate is stock REF2VA + PDD. Hybrid + standard sampling is the reference-influence control; hybrid + PDD is an experimental third gate.

## Verified

- Non-GPU tests cover serialization, UUID stability, legal and illegal transitions, path safety, retry preservation, draft promotion, missing/corrupt artifact reporting, interruption recovery, atomic-manifest residue, fingerprints, duration alignment, sigma validation, timeline ordering, continuation trimming, schema migration, anchor metadata/integrity, handover alignment, and prompt reinforcement.
- Custom-node registration imports successfully in the installed ComfyUI source.
- An embedded-Python runtime smoke composes existing `minimax_refs` with a frame-38 native `minimax_keyframes` guide and writes the UUID-backed PNG anchor.
- The MMH3 backend smoke passed under ComfyUI's embedded Python using current MMH3 source: Card 1 save, process restart, full archive load, 39-frame direct joint AV handover, and Card 2 save.

Automated checks do not measure whether the model obeys the visual state strongly enough. The documented cap/jersey A/B in the PDD REF2VA workflow is the required Stage 2A acceptance gate; the five-card and restart/Card-6 sequence remains the wider production validation.

## Deferred

- Branching and non-tail parent selection.
- Per-card T2VA/I2VA/L2VA/FL2VA/REF2VA switching.
- User-supplied first/last-frame guides beyond the automatic current-state anchor.
- Manual anchor picking, historical-anchor selection, CLSS, and additional anti-drift work.
- Bridge retakes, latent upscale, PDD upscale, prompt inheritance/composition, and custom decoder UI.
- Automatic embedding or copying of the reusable reference packet into each card archive.

## Known limits

- Project mode and resolution cannot change after creation.
- REF2VA resume requires the reference packet to be connected again before generating another card.
- The MVP exposes one active linear tail in the UI even though ancestry is stored separately.
- Preview context trimming uses the model's 24 fps timing to trim decoded audio samples.
- Timeline export requires an NVENC-capable FFmpeg and NVIDIA driver and exports at the model's fixed 24 fps.
- Model identity is recorded as a safe patch/model summary because Comfy `MODEL` patchers do not expose a universal immutable checkpoint identity.
- PDD-ACC compatibility with the REF2VA/FL2VA hybrid weights is not established by upstream PDD source or by the non-GPU tests.
- The native H3 guide exposes no strength control. Stage 2A records its mode but cannot tune guide strength independently.
- A single visible frame cannot preserve state that is occluded or outside the frame, and a strong older Ref2VA appearance can still win. The prompt reinforcement reduces that ambiguity but does not guarantee compliance.
- `source_timestamp_seconds` is local to the source card archive and includes any continuation prefix stored in that card.
