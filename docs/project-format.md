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
├── derivatives/
│   └── <source card UUID>/
│       └── refine_<derivative UUID>.mmh3
├── transactions/
└── previews/
```

`project_name` accepts 1–64 ASCII letters, digits, dots, underscores, and hyphens. Resolved artifact paths must remain under the project directory.

## Manifest

`project.json` schema version 12 contains project-wide fixed settings and a list of cards. Schema 1–11 projects migrate in place by adding missing anchor, identity-binding, publication-history, identity-scope, timeline-predecessor, publication-ID, structured-prompt, refine-derivative, continuation-source, per-card refine-policy, project LoRA activation, and draft-take fields; MMH3 masters are not changed. A completed legacy card becomes one selected take. The active card is selected by UUID rather than timeline position.

Project fields:

| Field | Meaning |
|---|---|
| `schema_version` | Manifest schema, currently `13`. |
| `project_name` | Safe directory and project name. |
| `revision` | Monotonic manifest commit counter. |
| `generation_mode` | `ref2va` or `t2va`. It may change only before the first card is rendered, then becomes fixed. |
| `refine_cadence` | `off`, `every_accepted_card`, or `manual`. |
| `lora_activation_words` | Exact project-wide trigger text injected at runtime into every newly generated card; it does not load or apply a LoRA. |
| `width`, `height` | Project generation canvas; both values must be multiples of 32. Resolution is editable before the first render or after every card has been invalidated. |
| `active_card_id` | Stable UUID of the current card. |
| `cards` | Timeline-ordered card records. |
| `active_identity_anchors` | Subject ID to active historical identity-anchor UUID bindings. |
| `pending_operation` | In-flight generation or refine marker, or `null`. |
| `last_operation` | Last completed, failed, or interrupted operation. |

Card fields include:

| Field | Meaning |
|---|---|
| `id` | Stable UUID. |
| `timeline_index` | Display/timeline order. It is not identity. |
| `artifact_number` | Non-reused accepted filename number. |
| `generation_parent_id` | UUID of the accepted generation source. |
| `timeline_predecessor_id` | UUID of the preceding assembled-timeline card; independent of generation ancestry. |
| `status` | `EMPTY`, `DRAFT`, `ACCEPTED`, `FAILED`, or `INVALIDATED`. A failed Retry retains the prior usable `DRAFT`; an invalidated card retains its definition but has no render lineage. |
| `prompt`, `assembled_prompt`, `prompt_hash`, `seed` | Assembled generation prompt compatibility value, canonical user prompt, its UTF-8 SHA-256, and seed. |
| `prompt_format`, `prompt_sections` | `structured_v1` six-section records or an untouched `legacy_flat` prompt, plus text/provenance records. |
| `continuation_strategy`, `reference_set` | Explicit continuation choice and the last graph-provided reference snapshot/summary. |
| `continuation_source` | Frozen source-card UUID plus `accepted_master` or a ready derivative UUID for this card's direct handover. |
| `continuation_source_preference` | Default source to select when the next direct-continuation card is appended. |
| `refine_enabled` | Manual-mode decision captured while the card is editable; ignored when project refine is Off or Auto. |
| `derivatives` | Rebuildable refine records; these never replace or mutate the accepted master. |
| `accepted_publication_id` | Stable UUID of the selected accepted take, or `null`. |
| `draft_inputs_dirty` | Whether prompt, duration, seed, continuation choice, or reference sizing changed after the current draft was generated; Accept is blocked until Retry commits a matching draft. |
| `draft_takes`, `selected_draft_take_id` | Immutable successful generation records and the UUID currently mirrored into the card fields. Retry appends a take; selection restores that take's exact inputs and artifacts. |
| `requested_duration_seconds` | Requested new timeline duration. |
| `ref_image_size` | Per-card REF2VA image sizing policy: `match` or `max`. Appended cards inherit the preceding card's choice. |
| `generated_frame_count` | Full sampled target, including continuation context. |
| `context_frame_count` | Preserved prefix; zero for Card 1 and 39 for direct continuation. |
| `actual_new_frame_count`, `actual_duration_seconds` | New timeline contribution after context removal. |
| `draft_path`, `master_path` | Relative MMH3 artifact paths. |
| `artifact_sha256` | Hash of the current authoritative artifact. |
| `generation_fingerprint` | SHA-256 of the canonical generation recipe. |
| `recipe` | Prompt, duration plan, model summary, compact upstream generation provenance, exact sigma values, sampler, parent hash, references, and runtime capability snapshot. |
| `anchors` | Persistent anchor records sourced from this card. These can contain automatic `current_state` and manual `identity` records. |
| `preview` | Optional disposable project-owned MP4 metadata registered from the card's current artifact. |
| `publication_history` | Superseded accepted versions retained when the latest card is unpublished. |

Each anchor record contains `anchor_id`, `source_card_id`, `source_frame_index`, `source_timestamp_seconds`, `role`, `asset_path`, `asset_sha256`, `media_type`, `created_at`, `enabled`, and `mode`. Identity records additionally contain `source_preview_frame_index`, `source_preview_timestamp_seconds`, `subject_id`, `label`, nullable `strength`, `identity_scope`, and nullable `custom_identity_instruction`. Valid scopes are `face_only`, `face_clothing`, `face_body`, `everything`, and `custom`; a custom scope requires non-empty instruction text. Older identity anchors migrate to `face_only`. User-facing frame selection excludes a card's continuation prefix; `source_frame_index` records the translated physical MMH3 frame. Both card and anchor identities are UUIDs.

The optional `preview` record contains `asset_path`, `asset_sha256`, `source_artifact_sha256`, `created_at`, `media_type`, and `fps`. It is derived review media and can be rebuilt or deleted without affecting the immutable MMH3 master or continuation. LongCaster accepts a registered preview only when its source hash matches the card's current draft or master artifact.

Each `draft_takes` record freezes a successful attempt number, artifact path/hash, prompt representation/hash, requested duration, seed, `ref_image_size`, continuation source, recipe/fingerprint, references, generated-frame measurements, optional preview, and creation time. Selecting a take restores its exact `match`/`max` choice along with the other inputs. The selected record is the only take eligible for acceptance. An unselected take and its unshared preview may be deleted; the selected take must first be replaced by another selection. Accepted cards retain their alternatives for inspection, but must be unpublished before another take can be selected.

The recipe's optional `upstream_provenance` record snapshots the submitted nodes feeding the first-pass `model`, `clip`, video/audio VAE, `sigmas`, and reference-packet inputs. It stores node types, connections, safe widget values, branch membership, an extracted model/LoRA/model-path summary, and a graph SHA-256; it never stores tensors or model bytes. Refine-only branches are excluded because they do not produce the draft. Sensitive input names are redacted, unusually large values and graphs are bounded with `truncated=true`, and older takes without execution provenance remain valid.

Each refine derivative records its UUID, source card/master path and hash, cadence, `PROCESSING`/`READY`/`FAILED` status, exact refine recipe, output path/hash, timings, prefix-protection diagnostics, audio behavior, and error. A ready derivative can become the next card's frozen continuation source. A failed regeneration preserves any previous ready derivative. Refine publication uses a separate artifact path and revalidates the accepted source hash before committing. Unpublish removes all refine derivatives and derivative metadata for that card while retaining the immutable accepted master in publication history.

Generation ancestry and timeline order are separate fields. The UI appends a linear tail. An editable tail may switch from `direct_mmh3` to `independent`, which clears `generation_parent_id` while retaining `timeline_predecessor_id`; switching back restores the accepted predecessor as its generation parent.

Structured prompt sections are stored in the fixed order `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, and `non_diegetic_music`. Each contains exact `text` plus provenance: `source_type`, nullable `source_card_id`, and `modified_after_copy`. The deterministic assembler in `longcaster/prompt_sections.py` owns the plain labeled format and prompt hash. Runtime state/identity reinforcement creates `effective_prompt` in the generation recipe without mutating these saved user sections.

An ambiguous legacy flat prompt remains byte-for-byte unchanged with `prompt_format=legacy_flat`. Obsolete XML-wrapped prompts are parsed only for migration. Structured prompts are assembled with the six plain `section_name:` headings MiniMax H3 expects. Explicit user conversion safely splits ordered canonical text headings, accepting underscore, space, or hyphen separators; otherwise it puts the full text in `detailed_description`. Conversion replaces the legacy representation only after confirmation.

Each `publication_history` record has a stable `publication_id`, the old `artifact_number`, immutable `master_path` and SHA-256, acceptance and invalidation timestamps, and snapshots of that publication's anchors, preview, prompt sections/hash, ancestry, seed/duration, continuation/reference data, generation fingerprint, and recipe. Unpublishing does not rename, overwrite, or delete the old master.

Removing an appended unaccepted tail is a separate destructive operation. It is permitted even when that draft came from unpublishing an accepted card. After confirmation, LongCaster removes the entire card record and its current draft plus all superseded publication masters, derivatives, anchors, and previews, then restores the preceding accepted card as active. The first card cannot be removed.

Invalidating a render keeps the card UUID, timeline position, prompt sections, duration, seed, and continuation choice, but permanently removes its current master/draft, all retained takes, previews, refinements, anchors, and superseded publications. It clears frozen recipe/output measurements and changes the card to `INVALIDATED`, from which Generate Draft can run again using the currently connected model graph. Only the active final card may be invalidated; a card with any later or dependent card is rejected. Invalidating an accepted card reserves a new artifact number so an accepted filename is never reused.

Project-wide invalidation performs the same render-lineage removal for every card in one guarded operation, preserves all card definitions, reserves fresh artifact numbers, and makes Card 1 active. Resolution editing then unlocks in the Project tab. Regeneration proceeds in timeline order: after an invalidated card is regenerated and accepted, the next preserved invalidated card becomes active and its direct continuation source is rebound to the newly accepted predecessor. Resolution locks again as soon as regeneration begins.

Duplicate-and-invalidate creates an independent project containing the source settings and complete card-definition timeline without cloning any render artifacts. All copied cards start `INVALIDATED`, artifact numbering restarts at one in the new project, identity bindings are cleared, Card 1 is active, and resolution editing is immediately available. The source manifest and assets are never modified.

## Commit and recovery rules

- Manifest writes use a unique same-directory temporary file, flush and `fsync`, then `os.replace`.
- A project lock serializes mutations across threads and processes.
- Draft generation sets `pending_operation` before sampling. A restart marks a surviving running operation as interrupted while retaining the previous draft.
- Stop Render / Unlock signals ComfyUI's render interrupt and commits the pending operation as `cancelled`. It does not alter the current card artifact; a late sampler result is rejected as stale.
- Retry writes a unique new draft. The old draft stays authoritative until the new archive passes full MMH3 verification and the manifest commits, then remains as an unselected draft take until explicitly deleted.
- Accept writes an intent journal, copies and hashes the master, refuses an existing destination, atomically publishes the file, then commits the card as `ACCEPTED`.
- Every-card refine starts only after that accepted-master commit. The derivative is fully verified before its manifest state becomes `READY`; failure leaves the accepted master authoritative and available for continuation.
- Resume completes a recoverable acceptance journal when the published master hash matches.
- Accepted filenames are never reused or overwritten.
- Unpublish is limited to the active accepted tail with no descendants. It copies that master to a retryable draft, archives the publication metadata, clears version-specific anchors and previews from the live card, and reserves a new artifact number for the next acceptance.
- Remove Draft is limited to the active, never-published tail after an accepted predecessor. It commits the shorter timeline first, restores that predecessor as active, then best-effort deletes the removed card's draft, preview, and cached anchor files. It never removes an accepted master or publication-history record.

Each accepted MMH3 contains its joint H3 sampler-output latent and LongCaster card metadata. Reference media stays in the separately connected reference MMH3 packet.

## Timeline export

Joined MP4 files are derived outputs under `ComfyUI/output/`, rather than project-authoritative artifacts. The exporter validates every selected archive hash, sorts accepted cards by `timeline_index`, and removes `context_frame_count` decoded frames and the corresponding 24 fps audio duration from each card. It may optionally append the active draft for review. It never edits accepted masters or `project.json`.
