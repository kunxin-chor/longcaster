# LongCaster project format

## Directory layout

```text
<ComfyUI output>/longcaster_projects/<project_name>/
├── project.json
├── .project.lock
├── clips/
│   ├── card_0001.mmh3
│   └── card_0002.mmh3
├── drafts/
│   └── card_0003_<attempt UUID>_draft.mmh3
├── anchors/
│   └── <source card UUID>/
│       └── <anchor UUID>.png
├── transactions/
└── previews/
```

`project_name` accepts 1–64 ASCII letters, digits, dots, underscores, and hyphens. Resolved artifact paths must remain under the project directory.

## Manifest

`project.json` schema version 6 contains project-wide fixed settings and a list of cards. Schema 1–5 projects migrate in place by adding missing anchor, identity-binding, publication-history, identity-scope, timeline-predecessor, publication-ID, and structured-prompt fields; MMH3 masters are not changed. The active card is selected by UUID rather than timeline position.

Project fields:

| Field | Meaning |
|---|---|
| `schema_version` | Manifest schema, currently `7`. |
| `project_name` | Safe directory and project name. |
| `revision` | Monotonic manifest commit counter. |
| `generation_mode` | `ref2va` or `t2va`. It may change only before the first card is rendered, then becomes fixed. |
| `width`, `height` | Fixed generation canvas; newly created projects require both values to be multiples of 32. |
| `active_card_id` | Stable UUID of the current card. |
| `cards` | Timeline-ordered card records. |
| `active_identity_anchors` | Subject ID to active historical identity-anchor UUID bindings. |
| `pending_operation` | In-flight generation marker, or `null`. |
| `last_operation` | Last completed, failed, or interrupted operation. |

Card fields include:

| Field | Meaning |
|---|---|
| `id` | Stable UUID. |
| `timeline_index` | Display/timeline order. It is not identity. |
| `artifact_number` | Non-reused accepted filename number. |
| `generation_parent_id` | UUID of the accepted generation source. |
| `timeline_predecessor_id` | UUID of the preceding assembled-timeline card; independent of generation ancestry. |
| `status` | `EMPTY`, `DRAFT`, `ACCEPTED`, or `FAILED`. A failed Retry retains the prior usable `DRAFT`. |
| `prompt`, `assembled_prompt`, `prompt_hash`, `seed` | Assembled generation prompt compatibility value, canonical user prompt, its UTF-8 SHA-256, and seed. |
| `prompt_format`, `prompt_sections` | `structured_v1` six-section records or an untouched `legacy_flat` prompt, plus text/provenance records. |
| `continuation_strategy`, `reference_set` | Explicit continuation choice and the last graph-provided reference snapshot/summary. |
| `accepted_publication_id` | Stable UUID of the selected accepted take, or `null`. |
| `draft_inputs_dirty` | Whether prompt/duration/seed changed after the current draft was generated; Accept is blocked until Retry commits a matching draft. |
| `requested_duration_seconds` | Requested new timeline duration. |
| `generated_frame_count` | Full sampled target, including continuation context. |
| `context_frame_count` | Preserved prefix; zero for Card 1 and 39 for direct continuation. |
| `actual_new_frame_count`, `actual_duration_seconds` | New timeline contribution after context removal. |
| `draft_path`, `master_path` | Relative MMH3 artifact paths. |
| `artifact_sha256` | Hash of the current authoritative artifact. |
| `generation_fingerprint` | SHA-256 of the canonical generation recipe. |
| `recipe` | Prompt, duration plan, model summary, exact sigma values, sampler, parent hash, references, and runtime capability snapshot. |
| `anchors` | Persistent anchor records sourced from this card. These can contain automatic `current_state` and manual `identity` records. |
| `preview` | Optional disposable project-owned MP4 metadata registered from the card's current artifact. |
| `publication_history` | Superseded accepted versions retained when the latest card is unpublished. |

Each anchor record contains `anchor_id`, `source_card_id`, `source_frame_index`, `source_timestamp_seconds`, `role`, `asset_path`, `asset_sha256`, `media_type`, `created_at`, `enabled`, and `mode`. Identity records additionally contain `source_preview_frame_index`, `source_preview_timestamp_seconds`, `subject_id`, `label`, nullable `strength`, `identity_scope`, and nullable `custom_identity_instruction`. Valid scopes are `face_only`, `face_clothing`, `face_body`, `everything`, and `custom`; a custom scope requires non-empty instruction text. Older identity anchors migrate to `face_only`. User-facing frame selection excludes a card's continuation prefix; `source_frame_index` records the translated physical MMH3 frame. Both card and anchor identities are UUIDs.

The optional `preview` record contains `asset_path`, `asset_sha256`, `source_artifact_sha256`, `created_at`, `media_type`, and `fps`. It is derived review media and can be rebuilt or deleted without affecting the immutable MMH3 master or continuation. LongCaster accepts a registered preview only when its source hash matches the card's current draft or master artifact.

Generation ancestry and timeline order are separate fields. The UI appends a linear tail. An editable tail may switch from `direct_mmh3` to `independent`, which clears `generation_parent_id` while retaining `timeline_predecessor_id`; switching back restores the accepted predecessor as its generation parent.

Structured prompt sections are stored in the fixed order `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, and `non_diegetic_music`. Each contains exact `text` plus provenance: `source_type`, nullable `source_card_id`, and `modified_after_copy`. The deterministic assembler in `longcaster/prompt_sections.py` owns the plain labeled format and prompt hash. Runtime state/identity reinforcement creates `effective_prompt` in the generation recipe without mutating these saved user sections.

An ambiguous legacy flat prompt remains byte-for-byte unchanged with `prompt_format=legacy_flat`. Obsolete XML-wrapped prompts are parsed only for migration. Structured prompts are assembled with the six plain `section_name:` headings MiniMax H3 expects. Explicit user conversion safely splits ordered canonical text headings, accepting underscore, space, or hyphen separators; otherwise it puts the full text in `detailed_description`. Conversion replaces the legacy representation only after confirmation.

Each `publication_history` record has a stable `publication_id`, the old `artifact_number`, immutable `master_path` and SHA-256, acceptance and invalidation timestamps, and snapshots of that publication's anchors, preview, prompt sections/hash, ancestry, seed/duration, continuation/reference data, generation fingerprint, and recipe. Unpublishing does not rename, overwrite, or delete the old master.

## Commit and recovery rules

- Manifest writes use a unique same-directory temporary file, flush and `fsync`, then `os.replace`.
- A project lock serializes mutations across threads and processes.
- Draft generation sets `pending_operation` before sampling. A restart marks a surviving running operation as interrupted while retaining the previous draft.
- Stop Render / Unlock signals ComfyUI's render interrupt and commits the pending operation as `cancelled`. It does not alter the current card artifact; a late sampler result is rejected as stale.
- Retry writes a unique new draft. The old draft stays authoritative until the new archive passes full MMH3 verification and the manifest commits.
- Accept writes an intent journal, copies and hashes the master, refuses an existing destination, atomically publishes the file, then commits the card as `ACCEPTED`.
- Resume completes a recoverable acceptance journal when the published master hash matches.
- Accepted filenames are never reused or overwritten.
- Unpublish is limited to the active accepted tail with no descendants. It copies that master to a retryable draft, archives the publication metadata, clears version-specific anchors and previews from the live card, and reserves a new artifact number for the next acceptance.
- Remove Draft is limited to the active, never-published tail after an accepted predecessor. It commits the shorter timeline first, restores that predecessor as active, then best-effort deletes the removed card's draft, preview, and cached anchor files. It never removes an accepted master or publication-history record.

Each accepted MMH3 contains its joint H3 sampler-output latent and LongCaster card metadata. Reference media stays in the separately connected reference MMH3 packet.

## Timeline export

Joined MP4 files are derived outputs under `ComfyUI/output/`, rather than project-authoritative artifacts. The exporter validates every selected archive hash, sorts accepted cards by `timeline_index`, and removes `context_frame_count` decoded frames and the corresponding 24 fps audio duration from each card. It may optionally append the active draft for review. It never edits accepted masters or `project.json`.
