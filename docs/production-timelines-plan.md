# Production timelines and future editing

Status: proposed architecture; no runtime changes. Based on the current schema 22 implementation. No workflows were inspected or run for this analysis.

## Feasibility and confirmed folder rule

This is feasible, including projects with no timelines and projects containing both assigned and unassigned cards. It requires separating generation order from edit order; adding a folder field alone would leave important operations unsafe.

The confirmed rule is that a production timeline/folder can contain A → B → C as a latent continuation chain. Its first card either starts independently or continues from an accepted card in another production timeline. Later continuations stay in the folder. This rule describes generation membership, not the first clip visible after an editorial trim or reorder.

Use logical folders in the manifest. Renaming or moving a folder must not rename accepted archives, change hashes, or move files. Episode/scene folder nesting can be added later without changing generation ancestry.

## What the code already provides

| Foundation | Current implementation | Consequence |
| --- | --- | --- |
| Stable card identity | `project.py`: card UUIDs, generation parent IDs, publication IDs/history | New ordering can reference existing cards without renumbering their identity. |
| Separate ancestry fields | `generation_parent_id` and `timeline_predecessor_id` | A useful starting point, although several operations still infer one from the other. |
| Frozen generation inputs | Native recipes, selected sources, references, LoRAs, seeds and archive hashes | New placements should reuse immutable results without modifying their generation inputs. |
| Native and hires chains | `hires_project.py`, `hires_runtime.py` | Cross-folder continuation can follow actual parent IDs, with hires ancestry kept separate from native sources. |
| A single active render | Project locking, pending operation, active card | Keep one running job per project initially; browsing another line must not retarget an in-flight job. |

The main obstacles are concrete:

- `ProjectStore.append()` creates a global-list tail. `activate_next_invalidated()` advances through that list and can rewrite the next card's parent.
- Card editing chooses continuation sources from the displayed predecessor. Validation requires contiguous `timeline_index` values and previously encountered parents/baselines.
- `invalidate_render_tail()` operates on the entire global suffix. Unpublish and draft removal use global-tail restrictions. Those rules are unsuitable for independent production lines.
- Hires parent validation uses global indices, and stale propagation walks cards in storage order.
- Studio navigation, batch planning and warnings use global positions.
- `timeline.selected_cards()` and export collect accepted cards globally and trim only their stored continuation context. There are no clip occurrences or source ranges.
- Some unpublish/invalidation paths delete derivatives. Publication history alone therefore does not yet guarantee that every asset referenced by an old edit remains available.

These need explicit scopes and dependency traversal before exposing multiple editable lines.

## Proposed model

Keep three concepts distinct internally, even if the UI presents them together:

1. **Card and publication:** a generation request, its takes and immutable accepted results. Its recorded parent is the source actually used to generate it.
2. **Production timeline:** an optional named folder containing an ordered generation line. This is where the confirmed root rule applies.
3. **Edit sequence:** ordered clip occurrences pointing to accepted publications and visible frame ranges. A publication may appear repeatedly, in multiple sequences, or as several split clips.

Suggested additive fields, with final naming decided during implementation:

```text
production_timelines: [{id, name, card_ids, remembered_active_card_id}]
edit_sequences: [{id, name, clips}]
selected_production_timeline_id: null | UUID
selected_edit_sequence_id: null | UUID

clip: {
  id, card_id, publication_id, source_artifact_sha256,
  in_frame, out_frame
}

generation_source: {
  card_id, publication_id, artifact_kind,
  derivative_id, artifact_sha256
}
```

`card_ids` should be the authoritative production membership/order; do not also maintain an independently mutable membership field on cards. Initially a card belongs to at most one production timeline. Editorial reuse happens through clips, not duplicate production membership.

Keep `cards` as a stable project catalog and retain existing `timeline_index` fields for legacy compatibility. Never reorder that array to perform an edit. New cards can be appended to storage while being inserted anywhere in an edit sequence. Generation fingerprints must exclude names, folder placement and clip ordering.

Build a two-pass UUID index and validate an acyclic generation dependency graph, alongside the production membership rules. Multiple lines may branch from the same accepted source. Validate actual source references rather than assuming that a folder's position proves its dependencies are valid.

Pin incoming sources to the actual accepted publication and artifact hash, including a selected refinement derivative. A frozen reference must not silently resolve to the latest publication or fall back to a different source. Preserve the existing native/refine source choices; hires output remains excluded from native continuation input.

For newly authored production lines:

- A root is independent, or has an explicit accepted source belonging to a different production timeline.
- A non-root direct continuation uses the previous card in that production line. Branching from an earlier card creates another production line.
- Preserve image restart and independent-shot modes. Image restart records its source but retains its existing latent-chain reset behavior. Later independent shots can remain in a folder unless a stricter product rule is chosen.
- An unassigned legacy card remains valid. To use it as the external root source of a new named line under the strict rule, explicitly adopt it into a suitable source line first. Do not silently assign all legacy cards.
- Assignment, movement and root removal validate the resulting membership and ancestry. They must not rewrite accepted history to make an operation appear valid.

A production line can have a simple derived playback view before an edit sequence exists. Creating an edit snapshots explicit publication references. Subsequent edits and retakes must not silently replace those references.

## Legacy compatibility

Use an additive migration to the next schema version. Missing timeline/sequence collections mean empty collections; cards are not automatically assigned. Projects without these features retain current append, accept, retry, navigation, batch and export behavior.

Existing project files must load without manual edits. This means new code reads old projects; it does not imply an older plugin can open a newly saved schema version.

Preserve omitted-selector behavior for legacy projects. New scoped operations pass explicit production timeline or edit sequence IDs, and generation passes an explicit target card ID. An ambiguous export from a project with several named lines should require a selection rather than silently concatenate every branch.

Prefer route/UI selectors over new mandatory node inputs. If node inputs are needed, keep compatible defaults, widget restoration and null repair. Retain the recent hires-widget regression check.

Keep the project-wide resolution, generation mode and render lock initially. Production folders do not imply independent project settings or simultaneous GPU jobs. Separate the selected UI card, the remembered editable card for each line, and the immutable execution target of a queued operation.

## Future editor operations

Clip ranges use integer visible-frame coordinates, with an exclusive end: `[in_frame, out_frame)`. Stored handover context is excluded from this coordinate system.

| Operation | Safe first implementation | Additional generation capability |
| --- | --- | --- |
| Split | Replace one occurrence with two ranges of the same publication. No latent mutation or generation. | Continuing from the split point needs a separately validated mid-card handover mechanism. |
| Reorder/reuse | Move or copy clip occurrences. Preserve original parents and source hashes. | Flag newly adjacent seams for review; adjacency does not establish latent continuity. |
| Generate between A and B | Generate X from a chosen accepted source, then place A, X, B. | B still has its original source. For true X → B continuity, generate a replacement B and the required descendants. |
| Prepend | Generate X and place it before A. | Existing A is unchanged. Making A continue from X requires a new generation of A. There is no backward continuation operation today. |
| Bridge two lines | Create a new card with pinned left/right boundary references and explicit placement. | An incoming latent continuation plus an outgoing visual constraint is experimental; it is not two-parent latent continuation. |

For example, after changing an edit from `A, B` to `A, X, B`, B's accepted archive remains a valid recording of its original generation. Its seam with X is unverified. Keep media validity, dependency status for a requested regeneration, and editorial seam review as separate concepts.

Arbitrary editorial splits are practical. Arbitrary latent splits are not implemented by the current adapter: it exposes a single source handover, while H3 generation uses the `17k + 5` frame grid and current continuation uses 39 context frames. A future mid-card handover must establish valid latent boundaries, sufficient AV context, source-window semantics and exact audio alignment. It must not be implemented as an assumed-safe tensor slice. An image restart from a decoded frame can be offered separately, preserving its existing semantics.

The existing `docs/fl2va-middle-retake.md` explores endpoint-guided bridges. Its model/conditioning combinations still need manual GPU validation. Matching outgoing images does not establish matching motion, audio or latent history. Bridge constraints should therefore be separate from `generation_parent_id`, with explicit seam review and a fallback to ordinary cuts or regeneration.

## Retention, invalidation and hires

Reference-aware retention is a prerequisite for pinned editing. Track references from edit clips, generation sources, boundary constraints, baselines/anchors and derivatives. Deleting a clip or folder must not delete its source media. Referenced historical publications and their selected refine/hires artifacts must survive retakes and unpublishing.

Replace global-suffix reasoning in scoped operations with an impact calculation over actual dependencies. A replacement source may require regeneration for a newly requested chain, but old descendants and edits can remain usable against their pinned historical sources. Rebinding a chain to a new publication is explicit. Invalidation must not delete unrelated lines merely because their cards have higher storage indices.

Preserve existing reset/invalidation capabilities with reference checks and a concrete affected set. Distinguish clearing an editable render, scheduling dependent regeneration and permanently deleting unreferenced media. Whole-project reset remains an explicitly whole-project operation.

Hires follows actual generation ancestry across folder boundaries. Reordering clips must not rebuild or invalidate hires. Resolving a clip's hires output must select a derivative of its pinned native publication, not the latest derivative of that card. Historical derivative retention and lookup need extending before this is reliable. Native continuation continues to use native/refine sources; a bridge's outgoing boundary does not become a second hires parent.

Export decodes the selected existing native or hires sources and slices the requested visible ranges. Map these to archive coordinates using `context_frame_count + in/out`; remove context exactly once. Use a shared frame-to-sample boundary convention and cumulative output positions to avoid gaps or rounding drift when splitting and joining audio. Keep existing hires source preflight and ancestor validation, including ancestors outside the selected edit.

Project duplication must preserve all new references and remap artifact hashes wherever archive rewriting changes them. Recovery must restore the targeted line/card without relying on whichever folder is selected in the UI.

## Delivery plan

| Stage | Deliverable | Completion gate |
| --- | --- | --- |
| 1. Compatibility foundation | Additive optional metadata, stable catalog, two-pass graph validation, characterization of existing operations. | Old and unassigned projects behave as before; folder/edit metadata cannot change recipes or archive hashes. |
| 2. Production lines | Create/name/select/adopt lines; enforce root rules; explicit source and render targets; scoped append, navigation and batch progression. | Two independent lines and a cross-line continuation work without parent rewrites or cross-line state leakage. |
| 3. Publication safety | Pinned source resolution, reference retention, graph-based impact calculations, historical derivative lookup, duplication/recovery support. | Retakes or invalidation in one line cannot silently break another line's accepted sources or edits. |
| 4. Basic editor | Explicit sequences, clip reuse, reorder, trim, split, selected-sequence previews and native/hires export. | Frame-accurate cuts preserve AV timing; edits do not mutate generation ancestry or trigger unnecessary generation. |
| 5. Editorial generation | Generate between/prepend, branch creation and explicit downstream-regeneration choices. | A user can distinguish an editorial insertion from a newly generated continuous chain. |
| 6. Experimental continuity | Mid-card handover and guided bridges. | Manual model-specific GPU results establish supported boundary, duration, reference and audio behavior before enabling supported modes. |

Stages 2 and 3 should ship together before allowing destructive operations across multiple production lines. A first release can stop there: it provides useful production organization while establishing the contracts needed by the editor.

## Verification plan for implementation

Use focused CPU/unit and frontend checks for migration, ID/reference validation, cycle rejection, mixed assigned/unassigned cards, root rules, explicit job targets, independent batch progression, impact sets, retention and duplication. Cover splitting with adjacent exact frame/audio boundaries and repeated references to the same publication.

Preserve regression coverage for native generation modes, drafts/takes/acceptance, card references, identities/state anchors, templates, per-card LoRAs, seed/recipe restoration, color continuity, Stage3A refinement and source selection, hires ancestry and export, previews, cancellation/recovery and legacy node widget restoration.

Generation quality, native/hires continuity and experimental bridges remain manual tests. No workflow execution is part of this planning task.
