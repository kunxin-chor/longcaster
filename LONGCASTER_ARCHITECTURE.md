# MiniMax H3 LongCaster — MVP Architecture

Architecture investigation, 11 September 2026. No LongCaster production code is implemented in this pass.

## 1. Executive Summary

**The MVP is feasible.** Use MMH3 as the durable artifact format and direct continuation adapter; use ComfyUI's normal sampling lifecycle with the upstream `MODEL`. LongCaster owns cards, attempts, acceptance, project transactions, and resume. It does not own diffusion model loading or model patching.

The recommended MVP is one session node with a small card editor. Card 1 uses text-only T2VA conditioning with an FL2VA-compatible H3 model. Following cards use that same conditioning family and MMH3's direct joint AV latent handover. An accepted card contains one complete, independently loadable `.mmh3`, including the full sampler output and its context prefix. Timeline playback excludes the repeated prefix; the master archive retains it.

Three findings materially affect implementation:

1. **Do not put MMH3's H3 Sampling preset inside LongCaster.** `MMH3H3SamplingPreset.execute` rejects nonempty model patches and object patches. Its presets can apply acceleration adapters. MMH3's lower-level persistence and continuation functions do not require this preset.
2. **Extender's default context is 22 pixel/video frames, not 22 latent positions.** It represents seven video latent positions as conditioning guides. MMH3 direct handover uses a protected prefix in the sampled target, with a default of 39 video frames, twelve video latent positions, and 65 audio latent positions. These are different inference paths.
3. **Current and installed ComfyUI differ in H3 mask semantics.** Current upstream has H3-specific mask conditioning and clean-context injection; the installed revision lacks those additions. Pin and validate a compatible runtime before treating rendered continuation or PDD integration as established.

There is no source-level blocker requiring a new sampler, checkpoint format, or diffusion implementation. Rendered PDD continuation quality, patch combinations, and Windows crash recovery remain validation gates. Persistence does not solve long-generation drift.

## 2. Verified Findings

### Source baseline and evidence labels

**Verified** means inspected implementation at the revisions below. **Probe** means executed CPU checks against the downloaded MMH3 code. **Recommendation** describes proposed LongCaster behavior. **Open** identifies evidence still needed. File links below use immutable revisions; a named function is the relevant entry point, not a README claim.

| Repository | Inspected revision | Scope |
| --- | --- | --- |
| [einhorn13/mmh3_media](https://github.com/einhorn13/mmh3_media/tree/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9) | `612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9` | Current default branch; archive, H3 contracts, direct continuation, conditioning, packing, sampling preset |
| [tritant/ComfyUI_MiniMax_H3_Extender](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/tree/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d) | `28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d` | Current default branch; motion context, disk chain, iterative UX, project import/export, FL2VA handoff |
| [Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc](https://github.com/Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc/tree/311a65dd53832d8a5f8177a9d5fb923c09e35a90) | `311a65dd53832d8a5f8177a9d5fb923c09e35a90` | Current default branch; Apply, scheduler, head fusion, wrapper guards |
| [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI/tree/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a) | `1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a` | Current upstream; six relevant files covering H3 nodes/model, sampler, execution caching |
| Installed ComfyUI | `0f1fa67ad8a68b62c65ebc97a7bf485df2459c3a` | Local comparison of those paths, plus ModelPatcher |

Public repositories were downloaded into a temporary research directory. No installed package was updated. The existing `docs/investigation.md` and `docs/plan.md` were empty and remain unchanged.

### What an MMH3 archive actually stores

**Verified:** `.mmh3` is a ZIP/ZIP64 archive containing `packet.json`, resource payloads, and optional derived representations. The current packet schema version is 2. `MMH3Media.create` creates packet identity, timestamps, generation metadata, notes, primary resource bindings, resources, representations, history, and extensions. An archive is a general media packet: the extension alone does **not** guarantee a usable H3 continuation checkpoint. [MMH3 → `core.py` → `MMH3Media.create`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/core.py), [MMH3 → `constants.py`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/constants.py).

| Stored element | Exact storage/meaning | LongCaster requirement |
| --- | --- | --- |
| Joint AV latent | Safetensors; H3 `samples` splits into named `video` and `audio` tensors | Mandatory primary latent, from completed sampling |
| Latent dictionary fields | Tensor/NestedTensor fields get a layout descriptor; JSON-safe extras are preserved; unsupported objects raise an error | Preserve valid fields and record mask policy explicitly |
| Noise mask | Can be serialized as a second nested field | Input masks describe a particular sampling operation; rebuild for every continuation |
| H3 provenance | Resource extension records origin, layout, stream shapes, canvas, timing, binding state | Require `sampler_output`, bound geometry/time, validated actual tensors |
| Decoded media | Video container, float32 WAV audio, PNG8 images, safetensors masks, JSON resources when supplied | Optional for continuation; useful for previews and review |
| Metadata/history | JSON generation settings, notes, resource metadata, operation records, extensions | Store the card's actual generation snapshot and lineage |
| Representations | Derived preview payloads with freshness metadata | Disposable; never generation authority |

`serialize_latent` copies tensors to contiguous CPU storage without requesting a different dtype. `deserialize_latent` reconstructs the latent dictionary and NestedTensor from stored layout information; `_load_owned_tensors` clones loaded storage so extracted files can be removed. Images are quantized to PNG8, so “lossless checkpoint” applies to latent tensor persistence, not every possible media representation. [MMH3 → `serializers.py` → `serialize_latent`, `deserialize_latent`, `_load_owned_tensors`, `serialize_image`, `serialize_audio`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/serializers.py).

**Verified:** `load_archive(path, verify="on_access")` reads and validates the manifest and ZIP members, returning an initially unmaterialized packet referencing the archive. `get_resource_payload` extracts and deserializes the selected resource. `verify="full"` reads and checks resource digests immediately; on-access verification checks the resource when materialized. Temporary extraction is an implementation detail, not the checkpoint's durable backing store. A lazy packet remains dependent on its archive path until its resources are materialized. [MMH3 → `archive.py` → `load_archive`, `materialize_resource_file`, `get_resource_payload`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/archive.py).

**Verified:** `save_archive` stages payloads, copies unchanged resources into the new archive, calculates resource digests/sizes, validates the completed temporary ZIP, then calls `_durable_replace`. That helper fsyncs the temporary file, replaces the destination, and attempts a parent-directory fsync. It is a single-file save, not a transaction with LongCaster's manifest. It can overwrite its destination: accepted-file immutability must be enforced by LongCaster. [MMH3 → `archive.py` → `save_archive`, `_durable_replace`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/archive.py).

### Sufficiency and restart behavior

**Verified:** a primary sampled H3 joint AV latent contains the video and audio values consumed by direct continuation. No prior Python object, GPU allocation, browser state, or Extender cache is needed. **Inference:** this is sufficient to start a new continuation operation after restart, provided the compatible inference stack is supplied again. It is not sufficient to resume an interrupted denoising step or reproduce an opaque model patch from metadata.

Reconstruct externally:

- The compatible H3 `MODEL`, all upstream patches, and their required configuration.
- The H3 text encoder/`CLIP` and the new card's prompt conditioning.
- Noise from the actual seed, sampler/guider, and exact schedule.
- Video/audio VAEs for review/export; the direct tail-copy operation itself needs neither VAE.
- Project order, acceptance state, pending attempts, and timeline trim metadata.

The archive stores neither diffusion/text/VAE weights nor executable ModelPatcher hooks. It does not automatically serialize a sampler's solver history or RNG object. Persist seed and inference provenance deliberately; do not call a seed an exact reproducibility guarantee.

**Probe:** a separate writer process saved a synthetic 124-frame, 32×32 H3 packet using `pack_h3_result` and `save_archive`. After that process exited, a second process loaded it with full verification and confirmed exact float32 video/audio tensors, nested masks, and JSON extras. It built a 158-frame continuation, verified copied prefix values, zero future latents, binary masks, independent target storage, and rejection of invalid origin/context/geometry/frame counts. The parent archive's SHA-256 remained unchanged. No models or GPU generation were used; the non-Comfy probe used MMH3's portable NestedTensor fallback.

## 3. MMH3 Continuation Data Flow

### Existing F02 path

```text
saved .mmh3
  archive.load_archive
    -> MMH3Media.get_primary("latent")
    -> h3_contract.h3_latent_contract_from_resource
    -> archive.get_resource_payload
    -> continuation.build_h3_continuation_handover
         -> fresh joint AV LATENT + nested noise_mask + H3ContinuationPlan

card prompt + dimensions + full target frame count + upstream CLIP
  MMH3H3ContinuationCondition.execute (family="fl2va")
    -> resolve_h3_continuation_family -> conditioning_mode="t2va"
    -> build_h3_expansion -> MiniMaxH3ImageToVideo (no keyframes)
         -> positive conditioning

upstream MODEL -> BasicGuider --------------------------+
seed -> RandomNoise                                    |
SAMPLER + supplied SIGMAS + fresh handover LATENT ------+
    -> SamplerCustomAdvanced.execute -> output LATENT
    -> process_result.pack_h3_result(origin="sampler_output")
    -> archive.save_archive -> independent draft .mmh3
    -> LongCaster Accept transaction -> accepted .mmh3
```

`MMH3H3ContinuationHandover.execute` selects the primary latent and checks its origin before materialization. The continuation conditioning node returns the fresh conditioning, seed, and process information; the latent to sample is the **handover output**, not the empty latent created inside its conditioning expansion. [MMH3 → `nodes_h3.py` → `MMH3H3ContinuationHandover.execute`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/nodes_h3.py), [MMH3 → `nodes_conditioning.py` → `MMH3H3ContinuationCondition.execute`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/nodes_conditioning.py).

For FL2VA-family continuation, `resolve_h3_continuation_family` explicitly chooses text-only T2VA conditioning: the latent prefix provides the start state. Card 1 can use the same native text-only conditioning and its empty latent. Select this family explicitly in the MVP, so adding arbitrary packet resources never silently changes generation mode. [MMH3 → `generation_contract.py` → `resolve_h3_continuation_family`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/generation_contract.py), [MMH3 → `comfy_h3_expansion.py` → `build_h3_expansion`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/comfy_h3_expansion.py).

### Direct handover mechanics

**Verified:** `build_h3_continuation_handover` accepts a sampled joint AV latent, requires batch one and the stock temporal grid, and rejects requested resolution changes. It allocates new zero-filled video/audio targets using the source tensors' dtype/device. It copies the source tails into target prefixes and makes separate full-shaped video/audio masks. Mask zero means preserve; mask one means denoise. The optional audio feather applies a half-cosine ramp within the copied audio tail. It neither decodes nor re-encodes the source, and it never concatenates the complete historical chain. [MMH3 → `continuation.py` → `build_h3_continuation_handover`, `H3ContinuationPlan`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/continuation.py).

For source 124 frames and target 158 frames, at the default 39-frame handover:

| Item | Video | Audio |
| --- | --- | --- |
| Source temporal length | 37 latent positions | 207 latent positions |
| Tail copied | Last 12 positions | Last 65 positions |
| Target temporal length | 47 positions | 263 positions |
| Protected target prefix | First 12 positions | First 65 positions |
| Future initialized to zero | Remaining 35 positions | Remaining 198 positions |

A longer context is possible only at valid boundaries and within both source and target duration. Earlier cards are not otherwise consulted. The checkpoint may contain other resources, but this handover function reads the selected joint latent only.

**Synchronization:** video is 24 FPS and audio latent rate is 40 Hz. The handover's video duration must simultaneously satisfy the H3 grid and an integral audio boundary: `39, 90, 141, 192, ...` video frames. Matching audio handover follows the video by default. Independently configured audio handover/feather lengths must map exactly to audio positions, meaning positive multiples of three video frames; zero feather disables feathering. Whole target/source audio lengths use rounding. Thus “exact handover” establishes equal context spans, not universally zero rounding error at the end of every source clip.

**Recommendation:** fix MVP handover to 39 video frames, equal audio span, zero feather. Require source duration at least 39 and target duration strictly greater than 39. The helper allows equal target/context duration; LongCaster should reject that because it adds no future. Validate H3 provenance bindings and the actual tensors, rather than trusting a filename or metadata flag alone. The compatibility validator also requires an explicit `absolute_start_frame`; use packet-local zero for these independent clip masters, keeping project placement and parent lineage separately. [MMH3 → `h3_contract.py` → `validate_h3_continuation_compatibility`, `build_h3_latent_contract`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/h3_contract.py).

### Sampling and resulting checkpoint

The helper does not generate random noise. `RandomNoise`/ComfyUI noise preparation and `SamplerCustomAdvanced` supply seed-driven noise and pass the nested mask to the guider. ComfyUI packs both streams and their masks for sampling, then returns the normal joint latent. Use the sampler's first `output`, after a completed schedule, rather than treating a preview callback's intermediate tensor as the checkpoint. [ComfyUI → `nodes_custom_sampler.py` → `RandomNoise`, `SamplerCustomAdvanced.execute`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy_extras/nodes_custom_sampler.py), [ComfyUI → `samplers.py` → `CFGGuider.sample`, `KSamplerX0Inpaint`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy/samplers.py).

Current upstream adds mask-derived per-row H3 timesteps and `MiniMaxH3.scale_latent_inpaint`: preserved video is injected near its conditioning timestep, and audio injection accounts for the carried audio variable. Consequently, direct copying into the target does not mean the model sees every copied value unmodified at every internal step. Let core own those semantics. The installed comparison revision lacks this H3-specific mask path. [ComfyUI → `model_base.py` → `MiniMaxH3.extra_conds`, `_denoise_mask_conds`, `scale_latent_inpaint`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy/model_base.py), [ComfyUI → `ldm/minimax/model.py` → `MiniMaxH3Model.forward`, `_forward`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy/ldm/minimax/model.py).

`pack_h3_result` validates the output latent, installs it as the primary latent, marks its supplied origin, derives geometry/timing, and stores process information. It operates on the packet passed to it and can retain unrelated prior resources. **Recommendation:** create a fresh packet per attempt with explicit card metadata, then pack that attempt's result. Do not use the parent's packet as an accumulating project container. This also avoids inheriting a stale decoded video or the parent's packet identity. [MMH3 → `process_result.py` → `pack_h3_result`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/process_result.py).

The sampler output retains the input `noise_mask`. For the MVP, remove that operation-specific field from a shallow copy of the **completed** result before packing; record the handover/mask policy in process metadata. Never remove it before sampling. Subsequent continuation always constructs new masks. Keep other serializable latent fields. This avoids presenting an old target-protection mask as an instruction for later operations.

### Duration contract

For full sampled clip length `N`, H3 uses:

```text
N = 5 + 17k, k >= 0                 video frames
seconds = N / 24
Tv = 2 + 5k                        video latent positions
Ta = round(N * 40 / 24)             audio latent positions
video shape = [1, 24, Tv, H/16, W/16]
audio shape = [1, 32, 2, Ta]
W and H are positive multiples of 32 for this MMH3 path.
```

Native `temporal_shape` snaps frame requests upward. MMH3's duration widget instead uses nearest-grid rounding. Neither is a universal seconds-to-frames rule: LongCaster must define one policy and pass the resolved count consistently to both conditioning and target construction. [ComfyUI → `nodes_minimax_h3.py` → `align_frame_count`, `video_latent_t`, `temporal_shape`, `_empty_av_latent`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy_extras/nodes_minimax_h3.py), [MMH3 → `nodes_settings.py` → `_duration_frames`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/nodes_settings.py).

**Recommendation:** expose **new timeline seconds per card**, plus a read-only resolved summary. Let `C=0` for Card 1 and `C=39` for continuation. Choose the nearest valid `N` to `24 * requested_seconds + C`, resolving exact ties upward, subject to `N>C`. Persist the request, `N`, `C`, and `new_frame_count=N-C`; actual durations derive from integers. Do not trim an archive's latent to the new-only duration: that duration usually does not itself lie on the causal H3 grid.

| Request | Full sampled frames | Video/audio latent lengths | Added timeline frames | Added seconds |
| --- | --- | --- | --- | --- |
| Card 1: 5 seconds | 124 | 37 / 207 | 124 | 5.166667 |
| Continue: 5 new seconds | 158 | 47 / 263 | 119 | 4.958333 |
| Continue with full target 124 | 124 | 37 / 207 | 85 | 3.541667 |

For initial UI support, use full target counts 124–362 on the valid grid and show the resolved limits in new seconds. This is a conservative product range based on the native node's training-range annotation, **not a quality guarantee or tensor-format maximum**. Algebraically smaller/larger counts exist. Use integer frame boundaries for delivery audio: cumulative PCM boundaries are `round(cumulative_frames * sample_rate / 24)`, so independent per-card rounding cannot accumulate timeline drift. Decode full masters, remove the continuation prefix only in derived playback/export, and explicitly validate audio length conformance. Equal latent context does not guarantee a seamless decoded cut.

## 4. Extender vs MMH3

### Existing continuation mechanisms

**Verified, motion-context path:** `MiniMaxH3Extender` prepares fresh target conditioning/latents, obtains the previous disk-backed sampled latent, and calls `MiniMaxH3MotionContextRAM.apply` before `_sample_h3`. `_video_tail_from_latent` divides the last context span into one-position latent blocks and supplies them as keyframe conditioning at appropriate frame offsets. It checks the `[1,4,4,4,4]` temporal phase. The default 22-frame span has seven video latent positions; choices are 5, 22, 39, and 56 video frames. [Extender → `extender.py` → `MiniMaxH3Extender`, `_sample_h3`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/extender.py), [Extender → `motion_context_ram.py` → `_video_tail_from_latent`, `MiniMaxH3MotionContextRAM.apply`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/motion_context_ram.py).

Audio comes from the same previous sampled AV latent. `_audio_tail_from_latent` selects rounded 40 Hz positions and retains a signed rounding residual to align the carried audio end. The native path supplies `audio_latent` keyframe conditioning; the compatibility path uses reference payload/layout patches. The motion-context continuation itself does not decode/re-encode. Decoding for preview/final output and the separate `MiniMaxH3TailFromLatent` utility should not be mistaken for that path.

**Verified, FL2VA alternative:** current Extender also supports a previous-frame image handoff. `resolve_fl2va_previous_frame` obtains a cached PNG or decodes the previous plan, selecting a continuity frame from its final six decoded frames. `make_fl2va_conditioning` VAE-encodes supplied first/last/guide images. Therefore, the claim “Extender never decode/re-encodes” is false across all its modes, although it is true for the sampled-latent motion-context handover under comparison. [Extender → `fl2va_engine.py` → `resolve_fl2va_previous_frame`, `_save_plan_last_frame_cache`, `make_fl2va_conditioning`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/fl2va_engine.py).

| Concern | Extender motion-context path | MMH3 direct path / LongCaster choice |
| --- | --- | --- |
| Previous state | Sampled AV latent read through disk proxy | Primary sampled AV latent from one archive |
| Video context | Conditioning blocks, default 22 video frames | Protected target prefix, fixed MVP 39 video frames |
| Audio context | Rounded tail with phase-aware placement | Exact-span tail copy, equal to video span |
| Future target | Fresh target guided by latent conditions | Fresh zero target with copied prefix and masks |
| Persistence | Shared binary chain, JSON offsets, derived sidecars | Independent `.mmh3` per accepted card, small project manifest |
| Retry | Rewrites candidate; causal suffix can be truncated | New attempt; parent and accepted masters untouched |
| Acceptance | Validated state with disk-presence checks | Explicit transaction publishing an immutable master |
| Assembly | Decode/seam correction and preview/export caches | Derived overlap-trimmed playback; no master concatenation |

### Cache, iterative UX, and resume

**Verified:** `motion_context_disk.py` stores a chain in `chain_<owner>.h3cache` plus JSON descriptors containing offsets, shapes, dtypes, frame counts, trim counts, and validation state. `_map_tensor` uses copy-on-write memory mapping; `_LazyDiskLatent` delays materialization. `_append_segment` appends raw video/audio tensor bytes and fsyncs them before manifest publication. `_truncate_chain` first publishes a retained manifest prefix, then discards the tail. `_recover_manifest` detects incomplete stored segments and recovers a safe prefix. Additional audio/preview/final-video caches support delivery. [Extender → `motion_context_disk.py` → `_chain_paths`, `_map_tensor`, `_LazyDiskLatent`, `_append_segment`, `_truncate_chain`, `_recover_manifest`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/motion_context_disk.py).

Its UX is not literally a Generate/Retry/Accept state machine. In `clip_by_clip`, execution stops after the first unvalidated candidate. Another active execution regenerates that candidate; seed modes advance candidate seeds. Checking **Validated** freezes/reuses a cached result; validated results missing from disk are errors, not silently regenerated. **+ Add Clip** appends a new configuration card. Full-batch mode also records computed checkpoints that can be reused before validation. [Extender → `motion_context_disk.py` → `MiniMaxH3MotionContextDiskJoin.join`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/motion_context_disk.py), [Extender → `web/extender.js` → `buildUi`, validation handlers, `onExecuted`, `advanceSeedAfterGenerate`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/web/extender.js).

Resume combines disk cache state with serialized workflow/card state and owner identity. The frontend's `restoreCacheState` reconciles disk information. After an interrupted batch, `resume_nonce` forces a graph-input change even for fixed seeds. Portable Save/Load Project additionally exports/imports `.ext` archives containing project information and cache/assets; `_import_project_archive` installs the data into the requesting node's cache and reconciles validation against physical availability. This is real restart/portability support, not only an in-memory cache. [Extender → `extender.py` → `_begin_batch_checkpoint`, `_build_project_archive`, `_import_project_archive`, `_replace_cache_transaction`](https://github.com/tritant/ComfyUI_MiniMax_H3_Extender/blob/28156b4a4e0450a6d8e2b426df8ed2d7e33bcd0d/extender.py).

**Preserve:** prompt/duration/seed cards, candidate preview with audio, explicit acceptance, reuse of accepted results, simple append, and reloadable project state. **Replace:** shared binary offsets, chain truncation, cache ownership tied to a graph node, mode-specific validity reconciliation, and cache migration needed just to recover accepted latent state. MMH3 still has extraction/preview caches, and LongCaster still needs transactions, validation, and preview invalidation. Independent archives simplify these responsibilities; they do not eliminate them.

## 5. Recommended LongCaster Architecture

### Small ownership boundaries

| Proposed component | Responsibilities | Excludes |
| --- | --- | --- |
| `project.py` | Manifest schema, IDs, paths, revision checks, locking, atomic manifest writes, recovery | Torch, model loading, browser state |
| `session.py` | Generate/Retry/Accept/Append commands, immutable attempt snapshots, state invariants, operation IDs | H3 tensor math and model patches |
| `mmh3_adapter.py` | Import/version boundary; load, inspect, materialize, prepare direct handover, pack, save, verify | Timeline ownership and UI callbacks |
| `sampling.py` | Native H3 text conditioning, seeded noise, supplied MODEL and SIGMAS, standard Comfy sampler lifecycle | Diffusion/LoRA/attention loading or reimplementation |
| `nodes.py` and local routes | One session node, generation queue integration, CPU-only project commands, UI results | An alternate background inference engine |
| `web/longcaster.js` | Card list/editor, four action buttons, preview/status, project reopen | Authoritative acceptance, file writes, tensor state |

These are suggested file boundaries, not a framework. Start with ordinary functions and small immutable records; split further only when implementation needs it. Pin the MMH3 revision used by the adapter, check schema/capabilities, and give an actionable dependency error. Do not silently import an arbitrary duplicate installation or copy the whole package into LongCaster. `public_api.py` exposes adapter version 1 and the continuation helpers; archive functions are also package exports. The import/registration mechanism on the chosen ComfyUI build is a Phase 1 check. [MMH3 → `public_api.py`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/public_api.py).

### MVP node contract

Required generation inputs: prepared `MODEL`, compatible H3 `CLIP`, project selector/path, operation ID, and expected project revision. Optional `SIGMAS` overrides the default scheduler. Expose sampler name, default scheduler, and steps as project generation settings; MVP guider is positive-only/CFG 1. The default non-PDD schedule comes from the supplied model's sampling object. External sigmas make steps/scheduler informational and inactive for that attempt.

Use one plain literal prompt per card. Call the native H3 text-only tokenization/encoding path, without adding prompt expansion or a multi-prompt language. The native conditioning node's VAE parameter is unused when no images are supplied; the sampling adapter can use its verified text-only operations plus native empty-latent creation without making a VAE necessary for tensor continuation. Decoding remains a separate consumer of the resulting packet, using externally supplied VAEs.

Outputs: latest published `MMH3_MEDIA` packet and compact status/project information. Return a lazy packet referencing the durable draft/master where practical, rather than retaining all previous tensors in ComfyUI output caches. A review path can use existing MMH3 extraction/separation and standard decoder nodes. Surface its derived video/audio preview beside the active card. Saving the generation must complete before expensive decode begins, so a decode error cannot destroy the completed draft.

No database, global tensor session store, mixed-mode scheduler, branch editor, or custom attention registry is needed. Keep only the selected parent's materialized latent, new target, and current sampler output alive during the relevant operation; release them after publication. Loading an MMH3 latent materializes its full resource, not only its tail, so measure host-memory cost rather than assuming Extender-like mmap behavior.

## 6. MODEL + SIGMAS Contract

### Preserve upstream MODEL behavior

**Verified:** ComfyUI `BasicGuider.execute` constructs its guider using the supplied model patcher. `CFGGuider` retains that patcher and its model options, invokes normal patcher preparation/pre-run/cleanup, and dispatches outer-sample, sampler, prediction, and diffusion wrappers. The H3 diffusion forward calls `WrappersMP.DIFFUSION_MODEL`. Thus the normal sampler boundary preserves the path used by compatible upstream patches. It does not imply that all patch combinations are mutually compatible. [ComfyUI → `nodes_custom_sampler.py` → `BasicGuider.execute`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy_extras/nodes_custom_sampler.py), [ComfyUI → `samplers.py` → `CFGGuider`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/comfy/samplers.py).

**Required LongCaster rule:** pass the supplied MODEL directly into that lifecycle. Do not reload a UNET, construct a fresh patcher around its diffusion module, clear object/weight patches, replace model options, install an attention backend, reapply recorded LoRAs, or call the diffusion forward directly. LongCaster need not clone MODEL merely to sample. Core may manage delegates/clones internally as part of normal patch handling.

The MMH3 archive and continuation helpers are model-independent. Their use does not strip MODEL modifications. The exception is the optional `MMH3H3SamplingPreset` integration: its rejection of patched bases applies even before choosing Standard/Custom. Exclude that node and MMH3's automatic optimization/adapter pipeline from LongCaster's internal path. Users may prepare a model upstream however they choose. [MMH3 → `nodes_optimization.py` → `MMH3H3SamplingPreset.execute`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/nodes_optimization.py).

This is the architectural answer for LoRAs, Singularity, sparse/alternative attention, acceleration patches, and future compatible H3 patches: preserve their ComfyUI execution boundary. Do not implement a patch-name allowlist or promise interoperability that their implementations do not support.

### What PDD-ACC actually does

`MiniMaxH3PDDAccApply.apply` loads the PDD artifact, resolves its partition, checks pairing where configured, clones the incoming model, and applies trunk LoRA patches via ComfyUI's patch APIs. Strength zero is a deliberate baked-trunk path. It fuses video/audio projection head banks using each stream's fine-grid interval weights, installs an object patch on `diffusion_model.final_layer.forward`, and registers a keyed diffusion wrapper. The wrapper records the active video sigma and validates shifts/audio scale; the final-layer patch selects the corresponding fused head and delegates to the native class forward. [PDD-ACC → `nodes.py` → `MiniMaxH3PDDAccApply.apply`, `make_wrapper`](https://github.com/Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc/blob/311a65dd53832d8a5f8177a9d5fb923c09e35a90/nodes.py), [PDD-ACC → `pdd_acc_core.py` → `fuse_heads`, `make_pdd_final_forward`, `select_block`](https://github.com/Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc/blob/311a65dd53832d8a5f8177a9d5fb923c09e35a90/pdd_acc_core.py).

Current core's final layer probes projection dimensions for native PDD-bank support. The inspected external PDD adapter already addresses this: `_PDDHeadLinear` forwards `weight`, `bias`, `in_features`, and `out_features` from the native projection, allowing an ordinary base head to take core's single-head path while calling the external fused projection. This is stronger evidence than signature compatibility alone, but still requires a render gate. [PDD-ACC → `pdd_acc_core.py` → `_PDDHeadLinear`](https://github.com/Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc/blob/311a65dd53832d8a5f8177a9d5fb923c09e35a90/pdd_acc_core.py).

Its grid is generated by `shifted_sigma(s,t)=s*t/(1+(s-1)*t)`, with 32 fine intervals for the inspected scheduler. `block_boundaries` selects video-grid knots matching the head partition. Eight evaluations use blocks of four; four use blocks of eight; six default to `8,8,4,4,4,4`. The Apply node returns the matching float32 SIGMAS directly. `MiniMaxH3PDDAccScheduler.get_sigmas` can emit the corresponding suffix for partial denoise; it must match Apply's partition. LongCaster should consume Apply's full schedule for the MVP's fresh future target.

PDD requires **Euler, CFG 1, video shift 12, audio shift 3**, and a matching model/PDD artifact. `MiniMaxH3SigmaShift.execute` configures both model sampling and transformer options; a SIGMAS tensor alone does not establish the model's audio mapping. The PDD wrapper rejects incorrect shifts or carried audio scale. Its core-support probe also checks for the carried-audio rework. Off-grid model evaluations default to an error, so a solver with extra intermediate evaluations cannot be assumed compatible merely because its input endpoints match.

Recommended graph:

```text
H3 loader
  -> MiniMaxH3SigmaShift(video=12, audio=3)
  -> PDD-ACC Apply / compatible upstream model modifications
       MODEL ---------------------------------> LongCaster MODEL
       SIGMAS --------------------------------> LongCaster SIGMAS

H3 CLIP loader -------------------------------> LongCaster CLIP
LongCaster packet -> review/decode nodes <- externally loaded VAEs
```

Apply is the owner of PDD math and head selection. Do not add a PDD implementation inside LongCaster. PDD's code warns against other distillation adapters and cache packs that skip its required calls; these are limits of that combination, not a reason for LongCaster to strip patches. Preserve the provider's errors and diagnostics.

### External schedule semantics

When SIGMAS is connected, pass its values/order/dtype to the standard sampler unchanged. Do not shift, regenerate, interpolate, normalize, truncate by a separate denoise setting, or prepend sigma 1. Core's transfer to the sampling device is expected and does not rewrite values. Record the exact schedule and its hash for the attempt. Reject invalid shape, nonfinite values, increasing values, or an incomplete/nonzero-terminal schedule for completed MVP master generation rather than silently fixing it. For this fresh-noise MVP, require full-noise start; future refinement can deliberately admit partial schedules.

**Inference:** binary MMH3 continuation masks do not introduce extra global sampler evaluations, so the Euler/PDD boundary schedule is structurally compatible. **Open:** PDD quality with protected H3 context is not established by those interfaces. Current core's preserved rows use different conditioning labels; an audio feather would add intermediate strengths. Keep feather off and test real PDD continuation. Do not equate successful tensor plumbing with the PDD training distribution.

## 7. Project Schema

### Files and identities

```text
project/
  project.json
  clips/
    card_0001.mmh3
    card_0002.mmh3
  drafts/
    card_0003_<attempt-uuid>_draft.mmh3
  previews/                         # disposable, created when needed
  transactions/                     # only pending publish/recovery records
  project.lock                      # OS-held writer lock, not a stale-file flag
```

Numeric filenames are allocated once and never reused or renamed when timeline order changes. UUIDs establish identity; filenames provide readability. The attempt suffix prevents a failed Retry from overwriting the currently displayed draft. A card still has only one current draft pointer and exactly one accepted master. Unreferenced superseded drafts can be cleaned after successful publication.

Example `project.json` below is illustrative; strings such as `sha256:<...>` represent real digests in an implementation. Seed strings prevent JavaScript precision loss for unsigned 64-bit values. Actual records include a complete generation recipe snapshot per published attempt; this example shows one shared recipe to reduce repetition.

```json
{
  "schema_version": 1,
  "project_id": "8d74ad22-064e-4f17-aa83-614c6ee8b050",
  "revision": 12,
  "name": "Harbour sequence",
  "created_at": "2026-09-11T03:00:00Z",
  "updated_at": "2026-09-11T03:15:00Z",
  "active_card_id": "3a2bf5ab-1095-49a4-bb17-ef4fb2874e30",
  "next_artifact_number": 4,
  "geometry": {"width": 896, "height": 512, "fps": 24},
  "duration_policy": "new_seconds_nearest_17k5_ties_up_v1",
  "continuation": {
    "strategy": "mmh3_direct_v1",
    "video_context_frames": 39,
    "audio_context_frames": 39,
    "audio_feather_frames": 0
  },
  "recipes": {
    "recipe_1": {
      "conditioning_family": "fl2va",
      "sampler": "euler",
      "guider": "basic",
      "cfg": 1.0,
      "sigmas_source": "external",
      "sigmas_dtype": "float32",
      "sigmas_artifact_hash": "sha256:<exact-tensor-bytes>",
      "sigmas_values": [1.0, 0.9882352948188782, 0.9729729890823364, 0.9523809552192688, 0.9230769276618958, 0.8780487775802612, 0.800000011920929, 0.6315789222717285, 0.0],
      "shift_video": 12.0,
      "shift_audio": 3.0,
      "model_identity": "<loader-artifact-identity>",
      "clip_identity": "<text-encoder-artifact-identity>",
      "upstream_graph_hash": "sha256:<relevant-upstream-graph>",
      "patch_provenance": ["<PDD artifact, partition and strengths>"],
      "provenance_complete": false,
      "runtime": {
        "comfyui": "1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a",
        "mmh3_media": "612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9",
        "pdd_acc": "311a65dd53832d8a5f8177a9d5fb923c09e35a90",
        "longcaster_adapter": "1"
      }
    }
  },
  "cards": [
    {
      "card_id": "1a12ac29-a9f3-42bc-9122-7d69dfc9a4a7",
      "timeline_index": 0,
      "status": "ACCEPTED",
      "prompt": "A boat crosses the harbour. Gentle engine noise and waves.",
      "duration_seconds": 5.0,
      "actual_frame_count": 124,
      "context_frame_count": 0,
      "new_frame_count": 124,
      "seed": "42",
      "mmh3_path": "clips/card_0001.mmh3",
      "mmh3_sha256": "sha256:<card-1-archive>",
      "packet_id": "<packet-uuid-1>",
      "primary_latent_resource_id": "<latent-resource-id-1>",
      "generation_parent_id": null,
      "generation_parent_sha256": null,
      "strategy": "t2va_v1",
      "recipe_id": "recipe_1",
      "attempt_id": "44c1f862-dd34-4253-8f20-6d50e94adfe2",
      "generation_fingerprint": "sha256:<card-1-generation>",
      "last_operation_id": "1d9e901a-9902-4aeb-9fcb-41f8029081cc"
    },
    {
      "card_id": "2e178ae6-6b6e-46be-8888-526ece931472",
      "timeline_index": 1,
      "status": "ACCEPTED",
      "prompt": "The boat slows beside the jetty. Its engine settles to an idle.",
      "duration_seconds": 5.0,
      "actual_frame_count": 158,
      "context_frame_count": 39,
      "new_frame_count": 119,
      "seed": "43",
      "mmh3_path": "clips/card_0002.mmh3",
      "mmh3_sha256": "sha256:<card-2-archive>",
      "packet_id": "<packet-uuid-2>",
      "primary_latent_resource_id": "<latent-resource-id-2>",
      "generation_parent_id": "1a12ac29-a9f3-42bc-9122-7d69dfc9a4a7",
      "generation_parent_sha256": "sha256:<card-1-archive>",
      "strategy": "mmh3_direct_v1",
      "recipe_id": "recipe_1",
      "attempt_id": "c5575a41-0b44-4221-a7ee-91b7f22868cc",
      "generation_fingerprint": "sha256:<card-2-generation>",
      "last_operation_id": "c75180ae-bb2c-413b-bf59-6a5f919cbac5"
    },
    {
      "card_id": "3a2bf5ab-1095-49a4-bb17-ef4fb2874e30",
      "timeline_index": 2,
      "status": "DRAFT",
      "prompt": "A dockworker catches the rope. Water laps against the jetty.",
      "duration_seconds": 5.0,
      "actual_frame_count": 158,
      "context_frame_count": 39,
      "new_frame_count": 119,
      "seed": "44",
      "mmh3_path": "drafts/card_0003_7376b8b2-a53c-4ed8-855a-73a0baaa7f7c_draft.mmh3",
      "mmh3_sha256": "sha256:<draft-archive>",
      "packet_id": "<packet-uuid-3>",
      "primary_latent_resource_id": "<latent-resource-id-3>",
      "generation_parent_id": "2e178ae6-6b6e-46be-8888-526ece931472",
      "generation_parent_sha256": "sha256:<card-2-archive>",
      "strategy": "mmh3_direct_v1",
      "recipe_id": "recipe_1",
      "attempt_id": "7376b8b2-a53c-4ed8-855a-73a0baaa7f7c",
      "generation_fingerprint": "sha256:<draft-generation>",
      "last_operation_id": "06aad04c-af3a-4d59-8514-1b1652b69da3"
    }
  ],
  "pending_operation": null
}
```

`actual_frame_count` always means full archive length; timeline length is `new_frame_count`. EMPTY cards have null actual/artifact/attempt fields until generation succeeds. Draft edits awaiting Retry live in a separate `next_attempt` configuration so they cannot relabel an existing candidate. Accept commits exactly the displayed attempt and its stored settings.

`generation_parent_id` is independent of `timeline_index`. Append initially chooses the selected accepted card as parent; loading and validation must not demand `parent == cards[index-1]`. Require an existing compatible accepted parent, no self-parenting/cycle, and a matching parent artifact hash. A later branch/retake can change timeline placement without rewriting how an existing artifact was generated.

### Fingerprint meaning

Hash a versioned canonical generation description: literal prompt, requested/resolved duration, seed, parent archive/resource identity, strategy parameters, geometry, effective schedule bytes/dtype/shape, sampler/guider settings, upstream model/text-encoder descriptors and relevant graph configuration, and adapter/runtime revisions. Keep the underlying recipe beside the hash. Exclude timeline index, preview settings, UI position, status, operation ID, and timestamps. A same-seed Retry can intentionally have the same generation fingerprint but a new attempt ID.

The fingerprint is provenance and comparison data, **not authority to regenerate, autoaccept, or discard an accepted artifact**. Arbitrary runtime patch objects may lack stable serializable identity. Mark incomplete provenance instead of hashing object addresses or inventing model names. Workflow configuration and available artifact digests improve reproducibility; they cannot reconstruct every opaque hook. Reconnecting a changed upstream graph applies only to new attempts.

## 8. State Machine

```text
EMPTY    -- Generate --> DRAFT
DRAFT    -- Retry ----> DRAFT
DRAFT    -- Accept ---> ACCEPTED
ACCEPTED -- Append ---> new EMPTY card (accepted card remains)

Resume -> reconstruct existing states; never implies Generate or Accept
```

ACCEPTED has no Retry transition. RUNNING/INTERRUPTED/FAILED belong to the pending operation, not to the last successfully published card artifact.

| Command | Preconditions | Successful effect | Failure/interruption |
| --- | --- | --- | --- |
| Generate | EMPTY; valid configuration; accepted compatible parent when needed | Publish a verified attempt archive and change card to DRAFT | Keep EMPTY; retain diagnostic/pending operation |
| Retry | DRAFT; explicit new attempt snapshot | Publish replacement draft pointer after successful save | Keep prior draft and preview selectable |
| Accept | DRAFT; matching attempt ID/hash and expected revision; no active generation | Publish one immutable master and set ACCEPTED | Recover transaction; never lose the draft first |
| Append | Selected card ACCEPTED; no unresolved active attempt | Allocate UUID, timeline index, artifact number, explicit parent; persist EMPTY | No partial card insertion |
| Resume | Existing project selected | Validate/reconcile; restore cards and last published attempt | Report missing/corrupt artifacts; no silent regeneration |

Generate uses the configured seed. Retry defaults to a newly selected seed, persisted before queueing; a fixed-seed choice intentionally repeats it. The actual completed seed stays with the candidate. Append may copy prompt/duration defaults for convenience, but creates a distinct card/attempt lineage.

Accepted files and their generation metadata are immutable. Retaking an accepted result is a future new card/artifact operation, not toggling acceptance off. Timeline annotations can evolve without modifying master bytes. Retry always reloads the accepted parent, never uses the current failed/rejected candidate as its own parent.

### Atomic publication and recovery

Use an exclusive project writer lock and optimistic manifest revision checks. Across processes, use an OS-held lock; a left-behind lock filename must not permanently block resume. Only one pending generation per project in the MVP. HTTP/browser state cannot bypass backend preconditions.

For Generate/Retry:

1. Validate request/revision; persist a pending operation containing operation ID, attempt ID, frozen settings, parent hash, intended draft path, and prior card state.
2. Execute only that snapshot. Write the candidate using MMH3's temporary-save/replace path to an attempt-unique name.
3. Fully verify the saved archive and required H3 primary resource; compute archive hash. Record card/attempt/fingerprint metadata inside the packet before saving.
4. Atomically replace `project.json` with the new DRAFT pointer and completion ID. Keep the old draft until this succeeds.
5. Clean only unreferenced attempt files after publication; cleanup failure does not invalidate a successful draft.

For Accept, validate the exact candidate that the user reviewed. Persist an acceptance transaction record with source/destination, hashes, IDs, and expected manifest revision. Copy the complete candidate bytes to a temporary file in `clips/`, flush and verify, then publish to a never-used final name with no-overwrite semantics. Atomically update `project.json` to ACCEPTED, then remove the transaction record and obsolete draft. Copying first costs temporary disk space but keeps the manifest's old draft reference valid throughout a crash. Do not reserialize the packet during acceptance or embed acceptance status by rewriting master bytes.

Recovery uses the pending record and artifact identity, not just directory scanning order. A complete verified draft can finish its pending draft publication; it is never automatically accepted. An acceptance interrupted after master publication can finish only when its recorded attempt/hash and manifest revision agree. Unrelated/orphan archives remain unreferenced until inspected. Never overwrite a conflicting accepted destination, and never truncate earlier masters to recover the current card.

Fsync/replace provides process-crash resilience; exact power-loss guarantees vary by filesystem and Windows rename behavior. Use same-filesystem staging, retain the journal until manifest publication, and fault-test each boundary. Validate all resolved relative paths at read/write time against the selected project root, including traversal and symlink/junction escape cases.

## 9. ComfyUI Implementation

### Python and execution caching

Use one public session node following the runtime's node API. Generation runs inside ComfyUI's normal queue execution, preserving cancellation, progress callbacks, model management, and upstream dependencies. The simplest MVP implementation calls native conditioning/guider/noise/custom-sampler behavior through `sampling.py`; dynamic graph expansion is available but unnecessary unless integration testing proves it preferable. Normalize V3 `NodeOutput` conventions only in this boundary.

Keep local project read/edit/Accept/Append routes CPU-only. They must not execute the diffusion model from an HTTP handler. Generate/Retry prepares a command snapshot and queues the node with literal project ID/path, operation ID, and expected revision. The backend atomically claims the operation when execution begins, checking that it has not been canceled or superseded. Changing the upstream graph between queue submissions affects the queued graph's inputs, which must be recorded in the attempt provenance.

Every explicit Generate/Retry gets a new operation ID even if prompt and seed are unchanged. Persist completion IDs and return already-published results on duplicate submissions. Distinct operations must not be collapsed by a settings-only Comfy cache key; repeated delivery of the same operation must not duplicate artifacts. Reject stale revisions rather than guessing which browser tab wins.

Implement V3 `fingerprint_inputs` (or the supported V1 `IS_CHANGED` hook) using literal inputs and disk manifest/selected artifact state. Include manifest revision, pending-operation status, and relevant artifact existence/stat information; verify content before consuming it. Do not try to inspect MODEL tensors from this hook. Current `IsChangedCache` intentionally obtains constants without cached upstream outputs. A fresh command ID drives generation invalidation; fingerprints also prevent a stale cached packet after external file removal or project updates. An always-changing value is not a substitute for an idempotent command protocol. [ComfyUI → `execution.py` → `IsChangedCache.get`](https://github.com/Comfy-Org/ComfyUI/blob/1d48d9cf7bcecb6022a87b3cb13e0fb435bf9b8a/execution.py), [MMH3 → `nodes_packet.py` → `MMH3Load.fingerprint_inputs`](https://github.com/einhorn13/mmh3_media/blob/612ddbccaa0cb591a5503f94ec66b7a9f44b4eb9/mmh3_media/nodes_packet.py).

An ordinary workflow Queue with no new pending command resolves the latest artifact/status and never creates another card or attempt. Reloading a workflow similarly reconnects to its project. Project identity must not derive from ComfyUI `UNIQUE_ID`; duplicated graph nodes may access the same project, subject to the writer lock and revision checks.

### JavaScript and review

Show a compact ordered card list, selected-card prompt/duration/seed editor, status, preview with audio, and Generate/Retry/Accept/Append buttons. Disable invalid transitions from authoritative backend state. Edits are persisted through revisioned project updates; local widget state is only a view. Freeze edits while a command is queued/running, or store them explicitly for the next attempt. Disable Accept on an edit whose displayed preview does not represent those settings.

Store only project location/ID, selected card, and transient view settings in the workflow. On browser/workflow load, query the backend manifest, reconcile pending transactions, and display the last published draft/master. Do not make workflow JSON the only copy of card prompts or acceptance state. A moved project can be reopened by selecting its new folder; relative artifact paths remain valid.

Preview cache keys include artifact hash, trim policy, and decoder identity/settings. Default candidate playback can show new frames with a small optional context lead-in, clearly separated in display metadata. Preview failure leaves DRAFT durable and offers decode retry. Accepted masters remain usable even when all previews are removed.

For export, process accepted masters in timeline order and trim each by its stored context count. Validate decoded frame count, audio sample rate, and duration before assembly. Context trimming is a derived presentation operation; do not directly concatenate latent tensors or assume latent equality guarantees a matching decoded seam. Basic trimmed playback is sufficient for the iterative MVP; advanced crossfades and seam correction are deferred.

## 10. Extension Points

These are ownership boundaries only; no future strategy is implemented or investigated here.

| Future feature | Attachment point |
| --- | --- |
| Attention/Singularity/acceleration patches | Upstream MODEL chain; existing Comfy patch lifecycle |
| Custom samplers/guiders/noise | `sampling.py` inputs/providers; session publication protocol unchanged |
| Nonstandard decoders/VAEs | Packet consumers and preview/export adapter; decoder provenance in derived-cache key |
| Alternative encoders, I2VA/FL2VA/Ref2VA | Conditioning/target preparation strategy; explicit card mode and resource inputs |
| Add Guide, historical injection, CLSS-style stabilization | A later continuation strategy returning conditioning, target, masks, timing, and provenance |
| Branch from older MMH3 | Explicit generation parent UUID/hash independent of timeline placement |
| Bridge retakes | New artifact with explicit source/target lineage; replace a timeline reference, not master bytes |
| Per-card latent/PDD upscale | Derived artifact/refinement stage with its own strategy, recipe, geometry and ancestry |

Do not expose unused future sockets now. Preserve original masters and make derived variants explicit. A future resolution-changing strategy must create a compatible continuation source deliberately; direct MVP continuation rejects a resolution mismatch.

## 11. MVP Implementation Phases

1. **MMH3 adapter and runtime contract.** Resolve the installed dependency; implement fresh packet packing, save/load, H3 contract validation, direct handover, and duration conversion. **Gate:** exact separate-process roundtrip; valid/invalid geometry and boundaries; no parent mutation; production runtime uses real Comfy NestedTensor. Reproduce the CPU investigation probe as focused adapter tests.
2. **Native sampling integration.** Build the smallest T2VA and direct-continuation graph/path using externally supplied MODEL, CLIP, and SIGMAS. **Gate:** full draft archive from Card 1; load it in a fresh ComfyUI process and generate Card 2; verify shape, masks, prefix preservation within numerical tolerance, audio decoding, cancellation, and no internal diffusion load. Confirm wrapper execution on the supplied patcher.
3. **PDD integration.** Wire SigmaShift 12/3 and PDD Apply's MODEL/SIGMAS into that path. **Gate:** actual eight-step Euler T2VA and continuation complete; observed model evaluations match supplied schedule; PDD wrapper/head selection survives; compare with the equivalent native Comfy graph. Assess audio/video handoff and fail on unsupported runtime rather than silently changing patches.
4. **Project storage and commands.** Implement schema, UUIDs, parent references, locks, attempt snapshots, Generate/Retry/Accept/Append, and journals. **Gate:** failures injected before/after each artifact/manifest publication boundary; duplicate commands and stale revisions; Retry/Accept never alter accepted parent hashes; restart retains prior draft after failed Retry.
5. **Minimal card UI and review.** Add project open/create, one-card editor, four actions, audio/video preview, and state synchronization. **Gate:** user can complete Generate → Retry → Accept → Append → Generate, close/restart ComfyUI, reopen the project, and continue with reconnected upstream inputs. Fixed-seed Retry must still execute; passive reload must not generate.
6. **Release acceptance.** Validate derived trimming/timing, missing/corrupt files, multiple tabs, project move/reopen, dependency diagnostics, host-memory use, and Windows publication behavior. **Gate:** at least three accepted independent archives, deletion of previews has no effect on continuation, parent/timeline distinction is supported by schema/service validation, and no master depends on a session cache.

Do not begin advanced modes, drift stabilization, batch scheduling, upscale, or a branch editor before these gates pass.

## 12. Risks / Open Questions

- **Installed runtime mismatch:** local ComfyUI is older than the current mask-aware H3 implementation. PDD's carried-audio probe alone does not verify that newer mask behavior. Choose the tested upstream revision or an explicitly tested equivalent; no environment update was performed here.
- **Rendered continuation quality:** source confirms tensor mechanics, not long-horizon stability, identity retention, lip sync, or a clean seam. Direct preservation and Extender conditioning are different; do not promise equivalent output.
- **PDD plus masks:** no full model generation was run. Binary masks with the trained Euler grid are the initial test target; fractional feather strengths remain disabled. Compatible patch transport is established structurally, not every patch combination.
- **Native head evolution:** current ComfyUI's H3 final-layer signature differs from the installed source. PDD's patch delegates through `*args/**kwargs` and forwards native projection attributes for the newer head-bank probe. Those adaptations are verified, but the exact pinned combination requires the Phase 3 render gate.
- **Archive identity and validation:** `.mmh3` can lack a latent or contain `vae_encoded`/`derived` state. Require the selected primary resource, bound H3 provenance, actual layout, expected rate/shape, and checksum before direct continuation. An origin string is declared provenance, not proof that someone actually sampled it.
- **Memory and disk:** MMH3 serialization/materialization can copy a whole card's latent; acceptance temporarily duplicates archive bytes. Measure peak host memory and disk space. Do not claim full random-access tail loading or zero-copy resume.
- **Audio rounding and decoder behavior:** stock-grid audio lengths round to 40 Hz. Frame count and PCM alignment must be verified with the actual decoder. Nonstandard decoders remain external; their output requires a compatible delivery contract.
- **Opaque upstream provenance:** not every patch can be fingerprinted or reconstructed automatically. Projects remain resumable with supplied compatible inputs; exact replay requires a sufficiently described upstream stack.
- **Import/runtime coupling:** MMH3 exports a public adapter surface but imports a broad package, and Comfy node registration may add optional backports. Validate how the dependency is imported/registered in the target installation; do not run unrelated optimization/backport application from LongCaster.
- **Crash durability:** file fsync and atomic replace are not a multi-file database transaction. Acceptance journaling, revision checks, no-overwrite publication, and real Windows fault tests are required. External deletion/corruption must be reported, never “recovered” by rerendering accepted work.

The investigation executed only lightweight CPU persistence/continuation checks. It did not run an H3 render, modify installed ComfyUI/custom nodes, or establish a benchmark. Those limits are the reason for the ordered validation gates, not reasons to redesign the inference stack.

## 13. Recommendation

**GO** for MVP implementation using MMH3's low-level artifact/continuation APIs and the standard ComfyUI sampling lifecycle. Exclude MMH3's patched-model-rejecting Sampling preset. Keep MODEL preparation and PDD logic upstream, and preserve external SIGMAS unchanged.

**Exact first implementation task:** implement `mmh3_adapter.py` for fresh packet creation, completed sampled-latent packing, `.mmh3` save/load, strict H3 source validation, and the fixed 39-frame direct handover; add its separate-process roundtrip and boundary tests. Complete that gate before building the session UI or attempting PDD generation.
