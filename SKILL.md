---
name: minimax-h3-longcaster-repository
description: Navigate and modify the ComfyUI-MiniMax-H3-LongCaster repository, including its ComfyUI nodes, MMH3 persistence, H3 conditioning, PDD sampling, continuity anchors, timeline export, frontend controls, and non-workflow tests. Use when investigating, implementing, reviewing, testing, or documenting changes in this repository.
---

# MiniMax H3 LongCaster repository guide

Use this file to find the source of truth for a LongCaster task before editing. Read only the files relevant to the change, then check adjacent tests and user documentation.

## Repository access boundary

The project root is the directory containing the root `__init__.py` file.

- Files and directories anywhere inside this project root may be listed, searched, read, created, edited, and otherwise accessed without requesting per-file permission or requiring the user to provide each absolute path.
- Do not access anything outside this project root unless the user gives explicit permission and provides the exact absolute path.
- Keep temporary files and generated test artifacts inside the project root and remove them when finished.

## Do not access workflows

Do not run or access workflow files, including repository examples and workflow-focused tests.

- Never inspect, enumerate, search, scan, parse, validate, compare, modify, or execute workflows.
- Do not inspect or run `tests/test_workflows.py` because it reads and validates workflow files.
- Continue only the parts of a task that do not require workflow access.

## Establish the current behavior

Use this precedence when descriptions disagree:

1. Read the executable Python and JavaScript for actual behavior.
2. Read `docs/project-format.md` for the persisted schema and filesystem contract.
3. Read `README.md` for the supported user workflow and operating instructions.
4. Read `docs/implementation-notes.md` and `docs/plan.md` for implementation status, limits, and pending validation.
5. Use `LONGCASTER_ARCHITECTURE.md` for design rationale and upstream research. It began as the architecture pass, so verify old proposals against current code.
6. Treat `docs/ui-brainstorm.md` as uncommitted ideas. Do not implement an item merely because it appears there.

Preserve these repository-wide contracts unless the requested change explicitly revises them:

- Keep model selection in ordinary ComfyUI nodes; do not hard-code checkpoints in Python.
- Pass the patched `MODEL` and externally supplied PDD-ACC `SIGMAS` through unchanged.
- Keep direct joint audio/video MMH3 latent continuation; do not introduce a VAE round trip into the handover.
- Never mutate or overwrite an accepted MMH3 master.
- Identify projects, cards, attempts, and anchors by stable IDs rather than display numbers.
- Keep automatic `current_state` anchors separate from manual `identity` anchors.
- Preserve native H3 conditioning structures such as `minimax_keyframes` and `minimax_refs`.
- Number picture, video, and audio references independently within their own media type.
- Ensure control-only actions do not unnecessarily decode media or load the video/audio VAEs.

## Top-level files

- `__init__.py` is the ComfyUI extension entry point. It registers the bundled Video Helper Suite format before importing nodes, exports node mappings, and declares the web directory. Refer to it when node registration, frontend loading, or encoder-profile discovery fails.
- `nodes.py` defines the public ComfyUI nodes and coordinates project actions, conditioning, sampling, anchors, decoding, and export. Refer to it for node inputs/outputs, button actions, execution flow, logging, UI-facing status, or node mapping changes. Keep reusable storage and inference logic in `longcaster/` rather than expanding this file unnecessarily.
- `README.md` is the user-facing setup and operating guide. Update it when installation, wiring, controls, supported workflows, tests, or visible behavior changes.
- `LONGCASTER_ARCHITECTURE.md` records the original architecture, verified upstream findings, state-machine rationale, and extension boundaries. Refer to it when evaluating a structural change or the reason behind direct MMH3 continuation, PDD passthrough, atomic persistence, or the MVP scope.
- `requirements.txt` documents standalone Python dependencies. LongCaster currently adds none beyond ComfyUI and sibling custom nodes. Change it only when runtime code truly introduces a package dependency.
- `.gitignore` lists generated or local files excluded from version control. Refer to it before adding caches, test artifacts, local projects, or generated media to the repository.

## Backend package

- `longcaster/__init__.py` exposes the small public backend API. Refer to it when another module should import a stable LongCaster helper rather than an implementation detail.
- `longcaster/duration.py` converts requested seconds to valid H3 frame counts and accounts for the 39-frame continuation context at 24 fps. Refer to it for duration rounding, frame-grid rules, or timeline-duration bugs.
- `longcaster/continuation.py` defines the continuation strategy boundary and the direct latent implementation. Refer to it when changing how an accepted parent becomes the next card's initial latent or when adding a future continuation strategy.
- `longcaster/fingerprint.py` canonicalizes generation recipes and computes deterministic fingerprints. Refer to it when generation inputs, provenance, cache identity, or reproducibility metadata changes.
- `longcaster/h3_runtime.py` builds native MiniMax H3 conditioning, combines persistent references and identity images, validates or creates sigma schedules, and invokes ComfyUI sampling. Refer to it for REF2VA/T2VA conditioning, reference injection, PDD/external-sigma behavior, sampler integration, or runtime capability checks.
- `longcaster/mmh3_adapter.py` isolates the `mmh3_media` API. It discovers the sibling package, loads and validates archives, materializes image/video/audio references, creates direct continuation handovers, and packs generated cards. Refer to it for MMH3 compatibility, archive I/O, resource ordering, latent contracts, or changes in the upstream `mmh3_media` API.
- `longcaster/project.py` owns schema versioning, project/card state, locks, atomic manifest commits, generation transactions, structured-card edits/copies, acceptance, tail unpublish/publication history, recovery, paths, artifact validation, anchors, and subject-to-identity bindings. Refer to it for any persistence, resume, locking, state-transition, migration, revision-conflict, or immutable-master change.
- `longcaster/prompt_sections.py` owns the canonical six-section prompt schema, exact deterministic assembly/hash, XML and labelled-flat parsing, full-prompt import assignment/fallback, default inheritance, copy provenance, and edit provenance. Keep prompt assembly out of routes, nodes, and frontend callbacks.
- `longcaster/preview.py` creates a project-owned NVENC card preview from an accepted MMH3 master when an older card has no VHS-registered preview. Refer to it for preview backfill or picker-video encoding failures.
- `longcaster/refine.py` owns Stage 3A cadence/seed validation, joint AV continuation-mask restoration, and fail-closed protected-prefix verification. Refer to it for same-resolution continuation derivatives; keep project persistence in `project.py` and Comfy sampling orchestration in `nodes.py`.
- `longcaster/routes.py` exposes lightweight server APIs for LongCaster Studio and the identity picker: project creation/discovery/settings, revision-checked card edits/copies, project-owned media playback, and historical identity controls. Refer to it when the interface cannot create or load a project, play a registered preview, save a card, detect a stale revision, or update anchor state without queueing generation.
- `longcaster/state_anchor.py` extracts and stores lossless PNG anchors, maps preview frames to archive frames, loads anchor images, applies the native current-state keyframe guide, and adds minimal state/identity prompt reinforcement. Refer to it for Stage 2A current-state continuity or Stage 2B manual identity continuity.
- `longcaster/timeline.py` selects cards for joining and removes stored continuation prefixes from their visible frame spans. Refer to it for accepted-card ordering, active-draft inclusion, or seam duplication and trimming issues.
- `longcaster/timeline_export.py` streams selected card video and audio through FFmpeg and H.264 NVENC. Refer to it for joined-video generation, encoder detection, CQ/preset/audio options, process failures, or export performance.

## Documentation

- `docs/plan.md` tracks milestone order, implemented stages, and required production validation. Refer to it when choosing the next planned task or recording completion of a milestone.
- `docs/implementation-notes.md` summarizes implemented decisions, verified behavior, deliberate deviations, deferred work, and known limits. Refer to it before changing PDD family pairing, project generation-mode locking, anchor behavior, or other established tradeoffs.
- `docs/investigation.md` records source-level checks of ComfyUI, MiniMax H3, PDD-ACC, Extender, hybrid loader, and `mmh3_media`. Refer to it when a change depends on an upstream API or when re-verifying assumptions after dependency updates.
- `docs/project-format.md` specifies the on-disk project layout, manifest fields, state transitions, anchor records, durability, and migration rules. Refer to it before changing `ProjectStore`, stored metadata, paths, or schema versions, and update it with every persisted-format change.
- `docs/ui-brainstorm.md` captures exploratory UI and future integration ideas, including safer project controls, storyboard behavior, previews, subject libraries, RefMod, OpenVDN, and FastH3. Refer to it during product planning; do not treat it as an approved implementation plan.
- `docs/stage-2c-cards-interface.md` is the approved Cards Interface + Structured Prompt Editor contract, schema/migration design, implementation map, exclusions, and acceptance gates. It overrides conflicting exploratory UI notes for Stage 2C.
- `docs/ref2va-longcaster-prompt-guide.md` is the model-facing Ref2VA rewrite contract, including LongCaster's runtime-only state/identity prompt reinforcement and rules for writing direct clip continuations. Use it when generating or reviewing six-section card prompts.
- `docs/fl2va-middle-retake.md` assesses middle-card replacement using native FL2VA boundaries, compares safe descendant invalidation with an experimental bridge that preserves downstream media, and proposes ancestry, duration, and seam-review rules. Refer to it before implementing card versioning, non-tail retakes, branching, or FL2VA bridge generation.

## Frontend and encoder profile

- `web/longcaster.js` adds LongCaster Studio plus ComfyUI project/identity controls. Refer to it for project creation/opening, full-prompt paste/import, full/section synchronization, safe flat conversion, autosave, section-copy UX, card/identity/reference inspection, previews, continuation choice, queue behavior, command IDs, interrupt/unlock behavior, dynamic titles, or widget interactions. Keep validation, assembly, provenance, and state transitions in Python because workflows can run through the API without this frontend.
- `video_formats/longcaster_nvenc_h264-mp4.json` is the Video Helper Suite encoder profile used by example preview nodes. Refer to it for NVENC codec, preset, CQ, pixel format, metadata, or AAC defaults. Restart ComfyUI after changing or adding a discovered profile.

## Workflow files

The repository's example workflows and `tests/test_workflows.py` are outside the permitted task surface. Do not open, inspect, modify, or execute them.

## Tests

- `tests/test_duration.py` covers H3 grid alignment, requested new duration, continuation context, and invalid durations. Run it after changing duration or frame-count rules.
- `tests/test_fingerprint.py` covers canonical recipe hashing and sensitivity to meaningful generation changes. Run it after changing fingerprint inputs or normalization.
- `tests/test_h3_runtime.py` covers the external sigma contract. Extend it for schedule validation and lightweight runtime-conditioning logic that does not require ComfyUI models.
- `tests/test_mmh3_adapter.py` covers discovery and normalization of the upstream `mmh3_media` API. Run it after dependency-discovery or adapter changes.
- `tests/test_project.py` covers the project state machine, locks, cancellation, transactions, recovery, migrations, anchors, immutable acceptance, artifact checks, and restart behavior. Run it after any `ProjectStore` or schema change.
- `tests/test_state_anchor.py` covers current-state and identity anchor indexing, selection, and minimal prompt reinforcement. Run it after changing anchor semantics or prompt injection.
- `tests/test_prompt_sections.py` covers exact six-section parsing/assembly, labelled-flat conversion, hashing, whitespace, inheritance, and provenance. Run it after any structured-prompt change.
- `tests/test_refine.py` covers synthetic joint AV mask restoration and exact prefix verification without importing ComfyUI or running a model.
- `tests/test_refine_project.py` covers refine persistence, immutable masters, fallback, cancellation, source selection, and frozen derivative lineage.
- `tests/test_timeline.py` covers card selection and continuation-prefix trimming. Run it after timeline or joined-export selection changes.
- Do not inspect or run `tests/test_workflows.py`.
- `tests/mmh3_backend_smoke.py` is a cross-process, non-GPU integration test for MMH3 save, full verification, restart, direct joint AV continuation, and repacking. Run it with `--mmh3-root PATH_TO_ComfyUI_mmh3_media` after changing continuation or archive integration.
- `tests/state_anchor_runtime_smoke.py` uses ComfyUI's embedded Python to verify native `minimax_keyframes` composition and current-state PNG persistence. Run it after Stage 2A or native-guide changes.
- `tests/identity_anchor_runtime_smoke.py` uses ComfyUI's embedded Python to verify historical-frame extraction and composition into native `minimax_refs`. Run it after Stage 2B or reference-conditioning changes.

Run only explicitly selected non-workflow tests. Do not use test discovery that would execute `tests/test_workflows.py`.

Use ComfyUI's embedded Python for runtime smoke tests because they import ComfyUI, Torch, and MiniMax H3 modules. Check frontend syntax after editing JavaScript:

```powershell
node --check web/longcaster.js
```

## Route common tasks

- For a controller action that triggers the wrong work, inspect `web/longcaster.js`, the corresponding action branch in `nodes.py`, and transaction handling in `longcaster/project.py`.
- For models or VAEs loading during Accept, Append, Resume, Cancel, or identity inspection, inspect early returns and output blocking in `nodes.py`, then the frontend action value and command ID in `web/longcaster.js`.
- For grain, sampling, or PDD differences, inspect `longcaster/h3_runtime.py` and compare against `docs/investigation.md` before changing sampler semantics.
- For missing or reordered references, inspect `materialize_native_references` in `longcaster/mmh3_adapter.py`. Remember that each media type has its own zero-based order and prompt numbering starts at one.
- For visible-state drift between adjacent cards, inspect `longcaster/state_anchor.py`, the anchor orchestration in `nodes.py`, and `tests/test_state_anchor.py` while preserving direct continuation.
- For facial-identity drift after an occluded card, inspect the identity selector in `nodes.py`, identity persistence in `longcaster/project.py`, and additional image-reference composition in `longcaster/h3_runtime.py`.
- For identity-picker playback, inspect `longcaster/routes.py`, the `LongCasterRegisterPreview` node in `nodes.py`, and the DOM video controls in `web/longcaster.js`.
- For Cards workspace save/copy failures, inspect the manifest revision and active-card state, then `ProjectStore.update_card_editor`/`copy_card_sections`, the `/longcaster/cards/*` routes, and the workspace dirty/flush logic in `web/longcaster.js`.
- For Studio project, identity, reference, or prompt-conversion issues, inspect the `/longcaster/cards/*` route payloads, the matching `ProjectStore` mutation, and the workspace render/event handlers in `web/longcaster.js`.
- For a project that remains locked, inspect cancellation and pending-operation reconciliation in `longcaster/project.py` together with `stopAndUnlock` in `web/longcaster.js`.
- For duplicated seam frames, wrong joined duration, or missing cards, inspect `longcaster/timeline.py`, `longcaster/timeline_export.py`, and card frame metadata in `docs/project-format.md`.
- For a missing custom node, inspect node mappings in `nodes.py` and extension import in `__init__.py` without accessing workflow files.

## Keep documentation synchronized

When behavior changes, update the narrowest authoritative document:

- Update `docs/project-format.md` for schema or persistence changes.
- Update `README.md` for anything users must wire, select, click, or understand.
- Update `docs/implementation-notes.md` for technical decisions, limitations, or verification results.
- Update `docs/plan.md` when milestone status changes.
- Update `docs/investigation.md` when new upstream source evidence changes an assumption.
- Add uncertain product ideas to `docs/ui-brainstorm.md` until the user promotes them into implementation scope.
