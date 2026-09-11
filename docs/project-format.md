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
├── transactions/
└── previews/
```

`project_name` accepts 1–64 ASCII letters, digits, dots, underscores, and hyphens. Resolved artifact paths must remain under the project directory.

## Manifest

`project.json` schema version 1 contains project-wide fixed settings and a list of cards. The active card is selected by UUID rather than timeline position.

Project fields:

| Field | Meaning |
|---|---|
| `schema_version` | Manifest schema, currently `1`. |
| `project_name` | Safe directory and project name. |
| `revision` | Monotonic manifest commit counter. |
| `generation_mode` | Fixed `ref2va` or `t2va` mode. |
| `width`, `height` | Fixed generation canvas. |
| `active_card_id` | Stable UUID of the current card. |
| `cards` | Timeline-ordered card records. |
| `pending_operation` | In-flight generation marker, or `null`. |
| `last_operation` | Last completed, failed, or interrupted operation. |

Card fields include:

| Field | Meaning |
|---|---|
| `id` | Stable UUID. |
| `timeline_index` | Display/timeline order. It is not identity. |
| `artifact_number` | Non-reused accepted filename number. |
| `generation_parent_id` | UUID of the accepted generation source. |
| `status` | `EMPTY`, `DRAFT`, or `ACCEPTED`. |
| `prompt`, `seed` | Card generation inputs. |
| `requested_duration_seconds` | Requested new timeline duration. |
| `generated_frame_count` | Full sampled target, including continuation context. |
| `context_frame_count` | Preserved prefix; zero for Card 1 and 39 for direct continuation. |
| `actual_new_frame_count`, `actual_duration_seconds` | New timeline contribution after context removal. |
| `draft_path`, `master_path` | Relative MMH3 artifact paths. |
| `artifact_sha256` | Hash of the current authoritative artifact. |
| `generation_fingerprint` | SHA-256 of the canonical generation recipe. |
| `recipe` | Prompt, duration plan, model summary, exact sigma values, sampler, parent hash, references, and runtime capability snapshot. |

Generation ancestry and timeline order are separate fields. The MVP UI appends a linear tail, while the schema does not infer a parent from `timeline_index`.

## Commit and recovery rules

- Manifest writes use a unique same-directory temporary file, flush and `fsync`, then `os.replace`.
- A project lock serializes mutations across threads and processes.
- Draft generation sets `pending_operation` before sampling. A restart marks a surviving running operation as interrupted while retaining the previous draft.
- Stop Render / Unlock signals ComfyUI's render interrupt and commits the pending operation as `cancelled`. It does not alter the current card artifact; a late sampler result is rejected as stale.
- Retry writes a unique new draft. The old draft stays authoritative until the new archive passes full MMH3 verification and the manifest commits.
- Accept writes an intent journal, copies and hashes the master, refuses an existing destination, atomically publishes the file, then commits the card as `ACCEPTED`.
- Resume completes a recoverable acceptance journal when the published master hash matches.
- Accepted filenames are never reused or overwritten.

Each accepted MMH3 contains its joint H3 sampler-output latent and LongCaster card metadata. Reference media stays in the separately connected reference MMH3 packet.

## Timeline export

Joined MP4 files are derived outputs under `ComfyUI/output/`, rather than project-authoritative artifacts. The exporter validates every selected archive hash, sorts accepted cards by `timeline_index`, and removes `context_frame_count` decoded frames and the corresponding 24 fps audio duration from each card. It may optionally append the active draft for review. It never edits accepted masters or `project.json`.
