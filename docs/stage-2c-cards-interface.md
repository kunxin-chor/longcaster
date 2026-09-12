# Stage 2C: Cards Interface + Structured Prompt Editor MVP

Status: implemented and expanded into LongCaster Studio project/identity/resource management; interactive ComfyUI and local GPU acceptance pending.

## Goal and acceptance path

Make long-form MiniMax H3 iteration practical enough to author, render, inspect, and accept 10–20+ cards. The milestone combines an all-cards project workspace with a persistent editor for the normal six-section H3 prompt:

1. `subject_definitions`
2. `summary`
3. `retention_analysis`
4. `detailed_description`
5. `overall_soundscape`
6. `non_diegetic_music`

The editor assembles those fields into the ordinary tagged H3 prompt. It does not introduce a model-specific side format or silently rewrite section text.

The validation path is: open a project; inspect its cards; open Card 7; jump to `retention_analysis`; copy selected sections from Card 6 or another accepted card; edit the new summary and detailed description; generate, retry, and accept; append Card 8 with useful inherited defaults; and repeat without reopening nodes or rebuilding normal graph plumbing.

## Implemented schema

`project.json` introduced structured card fields in schema 6; schema 7 changes their assembled model prompt to MiniMax H3's plain labeled format. Existing fields remain valid. These fields are present on each card:

| Field | Meaning |
|---|---|
| `timeline_predecessor_id` | Stable UUID of the preceding assembled-timeline card, separate from `generation_parent_id`. `null` for the first card. |
| `prompt_sections` | Map containing exactly the six ordered section records described below. |
| `prompt_format` | `structured_v1` for assembled prompts or `legacy_flat` for an unconverted historical prompt. |
| `assembled_prompt` | Deterministic user-authored six-section prompt before runtime anchor reinforcement. |
| `prompt_hash` | SHA-256 of UTF-8 `assembled_prompt`. |
| `continuation_strategy` | Initially `direct_mmh3` or `independent`; stored explicitly rather than inferred from card position. |
| `reference_set` | Backward-compatible snapshot/summary of the graph-provided references used or selected for this card. Stage 2C displays this but does not build the full asset manager. |
| `accepted_publication_id` | Stable UUID for the selected accepted take, or `null`. Existing publication history remains immutable. |
| `draft_inputs_dirty` | `true` when generation inputs changed after the current draft; blocks Accept until Retry produces a matching artifact. |

Keep `prompt` as a compatibility alias containing the exact assembled prompt used by existing controller and archive paths. New code must derive it from `assembled_prompt`; it must not become a second editable source of truth.

The persisted lifecycle states become `EMPTY`, `DRAFT`, `ACCEPTED`, and `FAILED`. `FAILED` means an empty card has no usable artifact after generation failed. A failed retry retains its previous `DRAFT` artifact and records the failed operation in `last_operation`/`last_error` instead of discarding the usable draft.

Existing `recipe` and generation fingerprint continue to snapshot model/config identifiers, sampler, exact external sigmas, references, prompt, parent hash, and runtime capabilities. Acceptance already records master path/hash and timestamps. Stage 2C exposes these fields rather than duplicating their authoritative values.

## Prompt-section schema and provenance

Each entry in `prompt_sections` has this shape:

```json
{
  "text": "User text is preserved exactly.",
  "provenance": {
    "source_type": "manual",
    "source_card_id": null,
    "modified_after_copy": false
  }
}
```

Valid `source_type` values are `manual`, `copied_previous`, `copied_card`, and `generated_internal`. Copying stores the source card UUID and resets `modified_after_copy` to `false`. The first subsequent text edit retains the original source and changes `modified_after_copy` to `true`. Clear produces empty text with manual provenance. Ordinary user entry uses manual provenance and a null source UUID.

Section records are validated server-side. The UI never supplies a card number as identity.

## Deterministic prompt assembly

Add `longcaster/prompt_sections.py` as the sole owner of:

- `PROMPT_SECTION_NAMES`;
- `empty_prompt_sections()`;
- `parse_legacy_prompt()`;
- `assemble_prompt()`;
- prompt hashing and section/provenance validation;
- default append inheritance and copy operations.

`assemble_prompt()` emits each section once in the order above, using MiniMax H3's plain labels:

```text
subject_definitions:
{exact section text}

...
```

Plain `section_name:` headings and separators are canonical; section text is not normalized, expanded, or rewritten. The returned string feeds the existing generation recipe and then the existing runtime anchor-reinforcement step. Anchor instructions remain runtime additions to `effective_prompt`; they do not mutate the saved user sections or `assembled_prompt`.

## Default append behavior

Appending a card creates a new UUID and sets both its timeline predecessor and generation parent to the current accepted tail under the MVP flow. It copies these sections with `copied_previous` provenance:

- `subject_definitions`;
- `retention_analysis`;
- `overall_soundscape`;
- `non_diegetic_music`.

It starts `summary` and `detailed_description` empty. Preferences for different defaults are later work.

## Migration from flat prompts

Schema 1–5 manifests migrate atomically under the existing project lock. MMH3 files and accepted hashes are never changed.

- If the legacy `prompt` contains one unambiguous instance of all six obsolete XML wrappers, extract their inner text for migration, mark the card `structured_v1`, reassemble it with plain headings, and compute its hash.
- Otherwise retain the original `prompt` byte-for-byte, mark it `legacy_flat`, and present it in a clearly labelled legacy editor/import view. Do not silently wrap or reinterpret it during migration.
- The first explicit conversion assigns one ordered, unambiguous set of canonical `section_name:` headings to the matching sections. If that safe split is unavailable, it places the full legacy text in `detailed_description`. Only that confirmed action switches to `structured_v1` and removes the historical flat-prompt panel.
- Populate `timeline_predecessor_id` from the prior timeline entry for existing linear projects and keep the existing `generation_parent_id` unchanged.
- Derive initial continuation/reference/publication summaries from current card fields and `recipe` where available; missing historical diagnostics stay null/unknown rather than being invented.

Generation of an untouched `legacy_flat` card continues to use its exact legacy `prompt`. All newly created or explicitly converted cards use deterministic six-section assembly.

## Cards UI approach

Extend the existing ComfyUI web extension with a full-size Cards workspace launched from the LongCaster project node. It remains tied to the current workflow node for queued sampling, while ordinary project/card reading and editing use lightweight server routes and do not load models.

The expanded layout contains:

- a project selector and card list with card number, UUID, status, prompt/title excerpt, and preview thumbnail;
- previous/next card navigation and an obvious active-card marker;
- six section tabs with one editor visible at a time;
- per-section Copy Previous, Copy From Card, Keep, and Clear controls;
- assembled prompt preview;
- duration, seed, fixed generation mode, continuation strategy, ancestry, active anchors, and reference summary;
- card preview video when registered;
- state-aware Generate, Retry, Accept, and Append actions.
- project creation and validated project-folder selection under the ComfyUI output root;
- identity history with readable source-card/frame labels and active binding controls;
- prompt-style media numbering for recorded MMH3 resources plus navigation to the connected reference graph;
- an accepted-card preview rebuild action through the connected identity node and Video VAE;
- side-by-side full-prompt and selected-section views with synchronized section positioning;
- explicit automatic conversion of complete canonical `section_name:` flat prompts.
- full-prompt paste/import for editable cards, with complete or partial canonical headings assigned to their sections and unlabelled text retained in `detailed_description`.
- a persistent execution panel below the card workspace showing the current node/stage, live sampling progress, bounded LongCaster log output, and execution errors or tracebacks, with a compact always-visible status row and a toggleable fixed-height scrolling log;
- a **Stop Generation / Unlock** header action that interrupts ComfyUI and queues the existing cancel operation so project locks are reconciled without closing Studio.

Edits autosave through revision-checked API calls after a short debounce and flush before generation. A dirty indicator remains visible until acknowledged. Closing, navigating, or queueing while a save failed produces an explicit warning. Keyboard focus and tab order make all six sections reachable without closing the workspace.

Generate/Retry first flush the active structured card, copy its assembled prompt and card inputs into the existing node widgets, and queue the existing graph. Accept and Append call the existing node actions. Server validation rejects edits to prompt inputs while a generation operation is pending and rejects edits that would mutate an accepted publication; editing an accepted card is inspection-only until a supported versioning action creates a new editable take.

## Server API and concurrency

Add project/card endpoints alongside the current identity routes:

- list validated projects and compact card summaries;
- read a complete project/card editor state;
- update editable card fields and sections;
- copy or clear one or more sections;
- stream an already registered card preview.

Every mutation includes the manifest `revision`. `ProjectStore` performs the change under its existing cross-process lock and returns HTTP 409 for a stale revision so two browser tabs cannot silently overwrite one another. Routes accept stable project names and card UUIDs and reuse existing path containment and manifest validation.

Card inspection/navigation is client-side state and does not mutate `active_card_id`. Controller actions remain restricted to the manifest's active generation card; older accepted cards are read-only inspection/copy sources in Stage 2C.

## Implementation map

| File | Work |
|---|---|
| `longcaster/prompt_sections.py` | New canonical section schema, parser, assembler, hash, inheritance, and provenance helpers. |
| `longcaster/project.py` | Schema 6 migration/validation; explicit predecessor, failed state, structured edits/copy, revision checks, inherited append, accepted publication ID, and reference/strategy metadata. |
| `longcaster/routes.py` | Cards project/read/edit/copy/navigation/preview endpoints and conflict responses. |
| `nodes.py` | Consume the persisted assembled prompt for generation; return richer Cards state; preserve current sampling, anchors, recipes, and action semantics. |
| `web/longcaster.js` | Cards workspace, six-section navigation, autosave/conflict handling, preview/status/reference panels, and existing action queue integration. |
| `docs/project-format.md` | Document the current project schema after implementation lands. |
| `README.md` and `docs/implementation-notes.md` | Usage, limitations, and validation sequence after implementation lands. |
| `tests/test_prompt_sections.py` | Exact assembly, parsing, hashing, provenance, inheritance, Unicode, whitespace, and invalid input tests. |
| `tests/test_project.py` | Migration, revision conflicts, lifecycle, ancestry, accepted immutability, and structured edit/copy tests. |
| `tests/test_workflows.py` | Workflow compatibility and node widget/action serialization. |

## Explicitly out of scope

Stage 2C does not implement Context Loop, Drift Control, color-stable drift, CLSS, full branch editing, middle-video bridge retakes, a polished timeline or waveform editor, prompt-writing/rewrite automation, automatic anchor scoring, face recognition, or a full asset library. Graph-provided reference packets remain supported and backward-compatible.

## Verification gates

- All current non-GPU tests remain green.
- Schema 1–5 fixtures migrate without changing accepted MMH3 files or hashes.
- Assembly is deterministic and preserves section text exactly.
- Revision conflicts and pending-generation edits fail safely.
- Failed retries retain the prior usable draft.
- Existing flat-prompt workflows still resume and generate unchanged until explicitly converted.
- The documented Card 7 to Card 8 workflow succeeds through the Cards workspace.
- A local GPU acceptance run reaches 10–20+ cards, restarts, resumes, and continues with correct anchors, references, prompt hashes, and AV seams.
