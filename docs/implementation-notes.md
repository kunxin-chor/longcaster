# MVP implementation notes

## Per-accepted-card H3 hires

`hires_project.py` supplies separate transactional bookkeeping; `hires.py` supplies pure target/protection contracts; `h3_latent_upscale.py` owns learned upscaler loading/unloading; `hires_runtime.py` reconstructs frozen conditioning and runs explicit Euler refinement. Stage 3A continues through its existing API. The vendored MIT network/normalization is from LBH's learned 3D upscaler, with its license retained in `longcaster/vendor/LICENSE.h3-upscaler`. No Timeline Director runtime is used.

Video must be H3 B×24×T×H×W, audio B×32×…; unsupported layouts fail. The network uses its training normalization, temporal chunks of 32 with replicate padding/overlap blending, and 32-pixel alignment. Checkpoints load safely and strictly. Comfy-managed models are unloaded before learned inference. The learned model is offloaded/released in a `finally` block before conditioning/refinement, with cache cleanup. No learned-model GPU cache is retained.

For a continued card, MMH3 reconstructs its protected mask at **target** dimensions. The reconstructed prefix must match the actual preceding hires tail byte-for-byte. A clamped (0.05) channelwise median DC offset is calculated/applied only on the disposable predicted prefix before that prefix is overwritten with the real parent values. The retained body is untouched by DC correction. All audio mask values are zero. Independent snapshots and strict dtype/byte equality detect in-place sampler corruption, rounding, or signed-zero changes, both after sampling and after archive reload. The native sampler/drift treatment is untouched.

Hires requires explicit MODEL and SIGMAS. Connect a base hires MODEL before per-card LoRA application: frozen LoRA names/strengths/hashes are loaded internally, allowing any accepted card to upscale while a different child remains active. The source recipe supplies effective prompt, reference resources, identity IDs, card image/mode/strength, state-guide target frame, templates, activation words, and seed. Card-reference clean-cache keys include the hires canvas. Native AddGuide receives the target latent so keyframes are encoded/resized for that canvas.

Studio separates latest attempt status from a previously ready hires result. Preview is a queued decode into a dedicated silent MP4; Timeline Export's `source=hires` verifies the complete required ancestry before decoding. It uses existing derivatives and the unchanged retained-frame/audio-trim rules. Auto-after-Accept is not implemented/enabled until visual seam validation succeeds.

CPU validation covers migration, creation/completion/failure/restart/cancellation, native-source exclusion, ancestry and artifact hashes, regeneration/deletion, strict inherited head/tail equality, all-audio locking, median/clamp behavior, frozen target conditioning, schedule forwarding, export preflight, and Studio control states. Unit tests use synthetic tensor/archive/Comfy boundaries, not production model inference.

Manual GPU validation remains required (no workflows were executed during implementation):

1. At native 544×960, accept one card, upscale to 1088×1920, and inspect corruption, color, banding, flicker and unchanged audio. Read logged upscale/refine timings, latent dimensions, chunk count and VRAM measurements.
2. Generate a direct second native card, accept and upscale using Card 1 hires. Export `hires`, inspect the trimmed join frame-by-frame for brightness/color/texture/motion/identity discontinuities. The runtime must also pass strict protected-prefix verification.
3. Extend to 3–5 cards and inspect accumulated seams.
4. Regenerate Card 3 hires: Cards 4–5 hires invalidate while all native masters and earlier hires remain valid.
5. Verify independent and Image Restart cards create new hires chains without requiring their preceding hires.

The defaults are designed for a 24 GB 4090 with Comfy offloading, but peak memory and output quality are **unmeasured** until these manual tests run. Temporal chunking bounds the learned upscaler; the high-resolution H3 refinement itself still depends on the connected model's memory/offload setup.

## Card-local Ref2VA pictures

Implemented one card-owned PNG input with Normal/Soften controls, native `<Picture N>` numbering, schema-15 migration, take restoration, authoring-asset lifecycle, and accepted-source refine reconstruction. Global packet ownership, direct 39-frame continuation, state/identity guides, and PDD MODEL/SIGMAS forwarding remain as before.

`h3_runtime.build_conditioning` invokes the current native ReferenceToVideo node with named arguments. A per-call VAE proxy lets native code perform resizing and RGB presentation while caching only the mapped card-image encode. After native conditioning returns, `substitute_card_latent` clones containers and replaces only that image block's latent; it checks the block actually contains the proxy's returned clean tensor. Unexpected native structure fails explicitly. Other block metadata and image/video/audio references are retained.

The cache is process-local and capped at eight entries / 256 MiB of CPU memory. Conservative VAE identity avoids unsafe cross-checkpoint disk reuse. Refine can rebuild a clean latent after restart; the recipe retains the original generation metadata, while the derivative's reference report records its reconstruction.

The integration was checked against upstream [ComfyUI ReferenceToVideo](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py) and the reference-latent concept in [neph1's nodes](https://github.com/neph1/comfyui-minimaxh3-condition-latent). Installed external files were not inspected. Tests exercise real CPU tensors and PNGs with a synthetic native node; live ComfyUI/VAE/Qwen, browser integration, GPU quality, and timing validation remain required. Studio labels an editable card's number as the last resolved packet value until the graph executes again.

## PDD family pairing

PDD heads must match the diffusion trunk entering PDD Apply. Stock REF2VA uses the Ref2VA PDD file. Stock FL2VA and FL2VA with the separate reference patch use the FL2VA PDD file. The reference patch supplies conditioning support; it does not change the underlying trunk family. Keep PDD `partition_check` set to `error`, since allowing a mismatch can silently degrade the result.

## Implemented

- Schema 22 adds separate hires derivatives and cadence. Schema 21 project state: schema-20 per-card LoRA state plus user-defined project prompt templates, deterministic runtime expansion, frozen template provenance, and draft invalidation for changed referenced definitions.

- Patch-agnostic upstream `MODEL` input and Comfy Basic Guider sampling.
- Optional external `SIGMAS`, with finite/decreasing/terminal-zero validation and direct forwarding to the advanced sampler.
- PDD-first default (`require_external_sigmas=true`) and a supported stock-REF2VA PDD example workflow.
- REF2VA projects with MMH3 image/video/audio reference resolution on every card, including direct-continuation cards.
- T2VA projects as the standard sampling baseline. A pristine first card may switch between T2VA and REF2VA; the selected project mode locks when rendering begins.
- MMH3 card packing, full archive verification, immutable accepted masters, and hash validation.
- Direct joint audio/video latent continuation behind `DirectLatentContinuation`, with a 39-frame handover and no VAE round trip.
- Per-card `image_restart`: a fresh joint AV latent guided at frame 0 by the accepted predecessor's persisted final-frame image, allowing a scene reset before later cards resume direct latent continuation.
- Stage 2A automatic current-state anchors: lossless final-frame PNG extraction at acceptance, UUID-backed card metadata, native H3 `MiniMaxH3AddGuide` conditioning at the handover boundary, and optional minimal prompt reinforcement.
- Stage 2B manual identity anchors: preview-relative frame selection from immutable accepted cards, project-level subject binding, lossless historical PNG persistence, native H3 `minimax_refs` conditioning without a spatial keyframe, and persisted prompt scopes for limiting attribute inheritance.
- Visual Stage 2B picker: server-side project/card discovery, an embedded project-preview player with playhead-to-frame synchronization, exact MMH3 frame confirmation, direct enable/disable/clear controls, and optional one-frame `IMAGE` input interoperability.
- LongCaster Studio workspace: project creation/opening under the managed output root, all-card inspection, registered preview playback/rebuild controls, readable ancestry/resource numbering, historical identity selection and control, state-aware controller actions, per-card continuation choice, side-by-side full/section prompt editing, full-prompt paste and automatic section assignment, section-level copy/clear, safe labelled-flat-prompt conversion, provenance display, debounced autosave, and revision-conflict protection.
- Project Interface authority: project selection synchronizes the controller, identity picker, and joined-timeline exporter; connected `project_state` inputs override stale project-name widgets at execution time, and stale results cannot switch the interface back to another project.
- Studio execution controls: a persistent bottom current-node/progress/status panel consumes ComfyUI execution events, receives bounded LongCaster logger output through a browser event, displays tracebacks in a toggleable fixed-height scrolling log, and exposes Stop Generation / Unlock through the existing interrupt-then-cancel path.
- Studio Card/Project views: card authoring and generation remain in Card, while Project presents identity history with accepted-card preview selection, exact frame/scope/label details, anchor images, green in-use state, and persisted bind/enable/disable/clear/delete/create controls. Delete unbinds every subject using the checkpoint before removing its record and project-owned image. Global execution and stop controls remain visible in both views.
- Stage 3A continuation refine: synchronized Off/Auto/Manual radio controls and a per-card Manual checkbox resolve acceptance-time policy with Off > Auto > checkbox precedence. Accepted cards can generate/regenerate a derivative unless Off; unpublish removes their refine files and metadata. Accepted masters remain immutable while same-resolution, one-pass joint-AV derivatives store reproducible sampling metadata, reconstruct both continuation masks, verify hard-protected video/audio values exactly, and fail safely without reverting acceptance.
- Project LoRA activation words: Studio stores one exact project-wide trigger string and injects it into the runtime `subject_definitions` prompt without mutating saved card sections. The effective prompt, recipe, and fingerprint capture it; changing it invalidates an existing draft. These project-wide words do not select or load a LoRA; global model/LoRA loading remains graph-owned.
- Project prompt templates: Studio lets users create project-local names such as `lighting` and `sound design`, edit their exact replacement text, preview the selected card's resolved prompt, and see unresolved-token warnings. Runtime expansion is deterministic and non-recursive, preserves saved sections, reserves native MiniMax labels, freezes used definitions and their hash in each recipe, and dirties only drafts that reference a changed name.
- Per-card model-only LoRAs: Studio persists an ordered stack of installed LoRA filenames, strengths, enabled states, and activation words. The separate Card LoRAs model node resolves and hashes those files before ModelSampling/PDD, while the controller verifies its project/card/stack receipt and freezes the resolved asset hashes in the recipe. A distinct refine model must pass through its own instance and receipt. Graph-owned LoRAs remain an additive baseline.
- Per-take generation provenance: the project node consumes ComfyUI's hidden submitted prompt and node ID, walks the first-pass MODEL/CLIP/VAE/SIGMAS/reference branches, and freezes a bounded JSON-only upstream graph snapshot in the recipe. Studio extracts and displays recognized diffusion models, LoRAs/strengths, and all remaining MODEL-path patch nodes for the viewed take. No model data or tensors are copied; secrets are redacted and abnormal values are explicitly truncated.
- Per-card REF2VA image sizing: Studio owns each card's `match`/`max` choice, synchronizes it to the graph for Generate/Retry, marks completed drafts dirty when changed, and restores the exact frozen value when another retained take is selected. Appended cards inherit the accepted predecessor's choice. The Take Settings panel displays this and the other frozen recipe inputs beside model provenance.
- Atomic project duplication: Studio copies the complete validated project into a staged destination, rebinds LongCaster metadata inside every referenced MMH3 card/refine archive, updates artifact and preview source hashes, and publishes the destination folder only after validation. Active operations and invalid source artifacts block duplication.
- Draft preview invalidation: a successful Generate or Retry clears the prior artifact's preview record, the preview registrar emits its new artifact/content hashes to the frontend, and Studio refreshes with a versioned media URL. Failed retries retain the prior usable draft and preview.
- Schema 6 structured prompt persistence: explicit timeline predecessors, stable accepted-publication IDs, prompt sections/provenance/hash, continuation/reference summaries, safe flat-prompt migration, inherited new-card defaults, and a `FAILED` state that never replaces a usable retry draft.
- Project-owned preview registration: reuse the existing VHS encode, copy it under the card UUID in `previews/`, and bind it to the exact draft/master artifact hash without changing the MMH3 master.
- Preview backfill for older projects: decode the visible accepted-card frames once and write a silent NVENC navigation MP4; the resulting identity anchor is still extracted separately from the immutable MMH3 source.
- Persistent UUID-based card state, ancestry, fingerprints, atomic manifests, locks, journals, restart reconciliation, and artifact diagnostics.
- Generate, Retry, Accept, Unpublish Latest, Remove Draft Tail, Append, Resume, and a small separate decode/trim adapter.
- Streaming joined-timeline export through H.264 NVENC. It reads accepted cards in timeline order and removes each recorded continuation prefix before encoding, with optional active-draft inclusion for review.
- Minimal controller buttons and PDD REF2VA plus standard T2VA example workflows. Both PDD reference workflows include the disabled joined-timeline exporter with authoritative `project_state` wiring.
- Official model filenames appear only as editable example-workflow loader defaults; the Python node has no checkpoint names or model-loading path.
- Workflow seed serialization includes ComfyUI's separate `control after generate` value, keeping width, height, steps, scheduler, sampler, and later widgets aligned.
- The PDD workflow serializes optional PDD controls explicitly and sets `partition_check=error`; LongCaster also rejects a non-Euler sampler when its external-sigma requirement is enabled.
- Non-GPU unit tests and a real MMH3 cross-process backend smoke test.

## Priority-driven deviation

The initial MVP brief allowed T2VA Card 1 and deferred mixed card modes. The production note prioritizes REF2VA, the REF2VA/FL2VA hybrid model, reusable references, and PDD-ACC. LongCaster sends the same reference packet into conditioning for every card in an REF2VA project. A pristine project can switch between T2VA and REF2VA, but it still does not permit card-by-card mode switching, so the original mixed-mode deferral remains intact.

PDD remains external. LongCaster does not copy PDD logic; it consumes PDD Apply's patched model and exact schedule. The external-sigma requirement is enabled by default but can be disabled for the standard workflow.

The installed PDD implementation states that PDD on a hybrid-merged trunk is untested. LongCaster deliberately does not suppress that experiment or discard the patched model, but the supported first gate is stock REF2VA + PDD. Hybrid + standard sampling is the reference-influence control; hybrid + PDD is an experimental third gate.

## Verified

- Non-GPU tests cover serialization, UUID stability, legal and illegal transitions, path safety, retry preservation, draft promotion, missing/corrupt artifact reporting, interruption recovery, atomic-manifest residue, fingerprints, duration alignment, sigma validation, timeline ordering, continuation trimming, schema migration, anchor metadata/integrity, handover alignment, and prompt reinforcement.
- Stage 2C non-GPU tests cover exact six-section parsing/assembly, Unicode and whitespace preservation, prompt hashes, inheritance and provenance, schema 1–5 migration, revision conflicts, accepted-card immutability, failed-generation state, and preservation of a usable draft after a failed Retry. The web extension also passes JavaScript syntax validation.
- Custom-node registration imports successfully in the installed ComfyUI source.
- An embedded-Python runtime smoke composes existing `minimax_refs` with a frame-38 native `minimax_keyframes` guide and writes the UUID-backed PNG anchor.
- The MMH3 backend smoke passed under ComfyUI's embedded Python using current MMH3 source: Card 1 save, process restart, full archive load, 39-frame direct joint AV handover, and Card 2 save.

Automated checks do not measure whether the model obeys the visual state strongly enough. The documented cap/jersey A/B in the PDD REF2VA workflow is the required Stage 2A acceptance gate; the five-card and restart/Card-6 sequence remains the wider production validation.

## Deferred

- Branching and non-tail parent selection.
- Per-card T2VA/I2VA/L2VA/FL2VA/REF2VA switching.
- Arbitrary user-supplied first/last-frame guides beyond the predecessor final-frame Image Restart mode.
- CLSS, automatic best-frame selection, face detection, multi-subject UI, and additional anti-drift work.
- Bridge retakes, latent upscale, PDD upscale, prompt inheritance/composition, and custom decoder UI.
- Automatic embedding or copying of the reusable reference packet into each card archive.

## Known limits

- Project resolution can change while the first card is pristine or after every card render has been invalidated. Generation mode can change only while the first card is still pristine; it locks after the first render.
- REF2VA resume requires the reference packet to be connected again before generating another card. The Studio can enumerate recorded resource kinds and IDs and focus the connected packet node, but it cannot edit media that was never persisted in the project manifest.
- The Cards workspace can inspect every card, but generation and editing still target one active linear tail even though ancestry is stored separately.
- Unpublish currently applies only to the latest accepted tail. The publication-history model is intended to support future per-card versioning controls and explicit descendant invalidation when editing a middle card.
- Preview context trimming uses the model's 24 fps timing to trim decoded audio samples.
- Timeline export requires an NVENC-capable FFmpeg and NVIDIA driver and exports at the model's fixed 24 fps.
- The final Comfy `MODEL` object still exposes no universal immutable checkpoint identity. LongCaster therefore retains both its safe runtime patch/model summary and the submitted upstream graph provenance; the latter records loader filenames and settings but does not hash model assets or prove the internal behavior of arbitrary custom nodes.
- PDD-ACC compatibility with the REF2VA/FL2VA hybrid weights is not established by upstream PDD source or by the non-GPU tests.
- The native H3 guide exposes no strength control. Stage 2A records its mode but cannot tune guide strength independently.
- Native H3 image references expose no independent identity strength. Stage 2B records `strength=null`; each subject can select one checkpoint, and enabled subjects are appended in deterministic order with card-local override policy. Identity scope is a prompt instruction rather than an attention mask, crop, or strength control, so full reference images can still leak excluded attributes or visually blend across subjects.
- A single visible frame cannot preserve state that is occluded or outside the frame, and a strong older Ref2VA appearance can still win. The prompt reinforcement reduces that ambiguity but does not guarantee compliance.
- `source_timestamp_seconds` is local to the source card archive and includes any continuation prefix stored in that card.
