# Generations and Timelines implementation plan

Status: UI separation, independent flat generation folders and read-only branch history are implemented in schema 24. Remaining stages are planned. This plan supersedes the folder/production-timeline terminology and delivery order in `production-timelines-plan.md`; that document retains useful editor and continuity research.

## Product decision

Studio has two workspaces:

- **Generations** produces and organizes footage. Folders group related work; branches expose continuation chains; cards contain shots, takes and accepted publications.
- **Timelines** assembles accepted publications into story order. Placement is independent of generation order, and the same publication can be used in several edits.

The current organizational timelines remain in a separate **Timelines window**, preserving their groups and assignments. **Generation folders** are a separate collection in the existing Cards/Generations interface. Neither collection is silently converted into the other. Timeline groups remain organization-only until the editorial stage below.

Folders are free organizational containers. The earlier rule that a root starts independently or continues from another line now belongs to **generation branches**, not folders: a branch begins independently or from an accepted source outside that branch. Two branches may live in the same scene folder. Moving a folder cannot change whether a continuation is valid.

Example:

```text
Generations
  Episode 01
    Scene 04 - Kitchen
      Main performance:      A -> B -> C
      Alternate reaction:         D -> E   (D generated from publication B1)
      Phone insert:          F             (independent)

Timelines
  Episode 01 - Assembly:     A1, F1, B1, D1, E1
  Episode 01 - Alternate:    A1, B1, C1
```

The suffix identifies an exact accepted publication. Placing D after B does not create ancestry; D's recorded generation source establishes it. Removing D from an edit does not remove D from Generations.

## Scope and rollout

Deliver the work in independently reviewable stages. Ship organization and readable history first. Actual branch generation and editable timelines require stronger source retention and dependency handling than the current global-list implementation provides.

Keep one render operation per project and the existing project-wide canvas and mode. Do not introduce concurrent GPU jobs, per-folder generation settings, cross-project source links, split generation, bridges, transitions or multitrack editing in this plan's initial releases.

### Stage 1: Generations organization and history browsing

1. Implemented: the Cards workspace is **Generations**, with independent generation-folder controls and clickable read-only source/descendant history. Existing timeline controls and memberships moved to the separate **Timelines window**. Schema 24 adds an empty generation-folder collection without changing existing timeline assignments.
2. Add optional nested generation folders for episode/scene organization in a subsequent additive schema migration. Reject folder cycles. Preserve current IDs, names and card assignments exactly.
3. Add optional authored shot names, separate from prompts. Display the existing stable card number and UUID link alongside the name. Do not renumber cards when sorted or moved.
4. Provide All shots, Unassigned, folder/subfolder browsing, search by name/number/UUID/prompt, and status filters for drafts, accepted, failed, invalidated and missing hires. Search spans collapsed folders and has a clear-filter action.
5. Add bulk folder assignment. Moving cards, renaming folders and removing folders preserve all render inputs and media. Removing a folder reparents its direct subfolders and cards to its parent; removing a top-level folder makes them unassigned. State this behavior in the action UI.
6. Add a read-only **Generation history** panel: independent/source type, source card, exact publication/hash when known, selected take/publication, clickable parent trail and direct descendants. Separate the live draft's proposed source from each completed take's actual source.
7. Add a local ancestry view for the selected card and its nearby relatives. Expand further on demand; do not draw the full project graph by default.

Completion gate: organizing a synthetic 250-shot project never changes generation parents, active execution target, fingerprints, media hashes or legacy export order. Old projects remain usable with no folders. History accurately distinguishes verified source references from incomplete legacy records.

### Stage 2: Exact source references and retained history

1. Introduce a uniform source reference on each newly generated take/publication: source card ID, accepted publication ID, native/refine artifact kind, derivative ID when applicable and artifact SHA-256. Image restart records its image/frame source while keeping its latent-chain reset semantics.
2. Freeze the source when scheduling generation, not when the user happens to browse the card. Record the resolved source in the completed publication. Retries restore their frozen inputs consistently.
3. Backfill old references only where existing publication/take records and hashes identify the source unambiguously. Missing or ambiguous provenance is shown as unresolved; never substitute today's accepted publication as historical fact.
4. Introduce shared publication/asset lookup for current and historical results. A pinned reference must resolve to matching bytes or report the missing source; it must not silently fall back to another derivative or publication.
5. Track references from descendant publications, timeline clips, refinement/hires derivatives, anchors and other source assets. Retakes, unpublish, invalidation and derivative replacement retain assets still referenced elsewhere.
6. Keep three statuses distinct: usable accepted media, whether a requested new generation chain needs rerendering, and whether an editorial seam has been reviewed. Accepting A2 does not make B1, generated from retained A1, corrupt.
7. Make deletion report its affected references. Folder removal is metadata only. Explicit project resets can still clear renders with their documented whole-project scope; partial deletion cannot silently break another branch or edit.

Completion gate: retrying, unpublishing or refining a source cannot silently redirect an existing descendant or destroy a publication referenced by another edit. Native continuation continues to exclude hires derivatives.

### Stage 3: Generation branches and targeted execution

1. Allow **Start independent shot**, **Continue this accepted publication**, and **Create alternate branch from here**. A new branch is created together with its first card. Branching from a historical source creates new cards; it never rewrites existing accepted parents.
2. A branch is a linear sequence of logical cards. Its first card starts independently or references an accepted source outside that branch. Each following latent continuation uses a publication of the preceding card. Independent starts create another branch; image restart explicitly marks a continuity reset.
3. Branch membership/order is not draggable generation history. Reordering belongs to Timelines. Branch labels and folder placement remain editable.
4. Existing cards stay unbranched until explicitly adopted. Adoption checks actual parent relationships, order and folder membership; unrelated cards must not be made into a chain by assignment. Unbranched legacy sources can remain valid external sources, visibly identified as such.
5. Store the remembered editable card per branch. Separate browsing selection from the card explicitly targeted by a queued job. Keep `active_card_id` as a compatibility execution cursor for old callers, rather than treating it as the only editable card in the project.
6. Refactor append, draft selection/editing, accept, retry, refinement, recovery and removal to accept explicit card/branch targets. Project identity bindings and other live inputs must be captured consistently for the scheduled target; browsing another branch must not change an in-flight recipe.
7. Replace global-index ancestry checks with two-pass ID indexing and cycle validation. Keep the global `cards` array stable for storage and legacy numbering.
8. Plan batches from an explicit branch and recorded source bindings. An unresolved external source blocks that branch with an actionable reason. Do not automatically reparent the next invalidated card to whichever card finished last.
9. Calculate regeneration impact from actual dependencies. Source retakes preserve the old chain; following a replacement publication is explicit. Never invalidate unrelated higher-index cards merely because they follow the source in storage.
10. Resolve hires ancestry through the actual native publication dependencies, including sources outside the current folder/branch. Remove positional assumptions without making editorial neighbors into hires parents.

Completion gate: two branches can each retain a draft; the user can resume either, generate one job at a time, and branch from an older accepted publication. Batches, cancellation, restart and invalidation affect the intended targets only. Branch generation must ship with Stage 2 protections.

### Stage 4: Basic editorial timelines

1. Extend the separate **Timelines** window with named editorial sequences. Keep its existing organizational groups until an explicit conversion/snapshot is requested. Create an empty sequence or explicitly snapshot selected accepted cards in the current project order.
2. Store clip occurrences with their own UUIDs, each pinning an accepted publication and source hash. A card can appear multiple times or in several sequences.
3. Support add, remove, duplicate occurrence and reorder. Initial clips use the full visible card duration. Reserve integer visible-frame ranges for future trimming; do not expose split, trim or transition controls yet.
4. Changing order changes only clip placement. Show source ancestry on demand and mark newly adjacent joins as unreviewed. Do not automatically regenerate footage to match an edit.
5. Accepting a newer publication does not update existing edits automatically. Offer an explicit replacement action showing affected clips and duration changes.
6. Preview/export the selected sequence in clip order. Native and hires export resolve the publication actually pinned by each clip. Ancestor checks may extend beyond the selected sequence. Remove continuation context once and use consistent frame/audio boundaries.
7. Preserve the explicit legacy project-order export. Add an optional sequence selector with a compatible default; do not concatenate all branches as an implicit editorial assembly. Label the selected export scope clearly.

Completion gate: editing `A1, B1, C1` to `A1, C1, B1` leaves every generation source and hires dependency unchanged. Two edits can retain different publications of the same shot. Existing project-order export still works.

## Proposed data contracts

Allocate schema versions at implementation time. New collections remain optional for legacy/unassigned projects. Use one canonical representation after migration, rather than maintaining two independently editable copies of folder membership.

| Record | Proposed fields and invariants |
| --- | --- |
| Generation folder | `id`, `name`, optional `parent_id`. Acyclic hierarchy; names unique among siblings. Renaming never moves artifact files. |
| Card organization | Optional `shot_name`, `generation_folder_id`. Stable card UUID and existing `timeline_index` retained. Organization fields are excluded from generation fingerprints and frozen prompt inputs. |
| Generation branch | `id`, `name`, ordered nonempty `card_ids`, optional `remembered_active_card_id`. One branch per card at most. Membership lives here, not in a second card-level branch list. |
| Branch folder placement | Derived from its member cards, which must share a folder. Moving a branch moves those assignments atomically. Moving one member to another folder is rejected with an explanation to move the branch; ancestry cannot be severed by a folder action. No duplicated branch folder field is needed. |
| Source reference | Source card/publication UUIDs, artifact kind, optional derivative UUID, artifact hash; appropriate frame metadata for image restart. Frozen for completed results. Legacy unresolved references remain explicitly identifiable. |
| Editorial timeline | `id`, `name`, ordered `clips`. Independent of folder/branch order. |
| Clip | `id`, `card_id`, `publication_id`, source artifact hash, visible `in_frame` and exclusive `out_frame`. Initially always the full visible span. Hires resolves a matching derivative of this pinned native publication. |

Generation relationships are a directed acyclic graph of actual publication dependencies. A branch is a navigational grouping of that graph, not a replacement for it. Historical B1 may remain bound to A1 even after the same logical A card selects A2.

Folder collapse state, search, filters and selection are browser preferences keyed by project. Persist meaningful project structure in the manifest; ordinary navigation must not cause manifest writes. Keep the running operation's explicit target separate from these preferences.

## Migration and compatibility

- Preserve schema-23 `organization_timelines` and `organization_timeline_id` in the separate Timeline window. Schema 24 adds independent `generation_folders` and optional `generation_folder_id` membership. Never convert one collection into the other automatically.
- Preserve unassigned cards, current card order, generation parents, selected takes, accepted publication IDs, recipes, references, paths and hashes. Create no branches or editorial timelines implicitly.
- Keep `/longcaster/cards/timelines` for timeline organization. `/longcaster/cards/generation-folders` manages generation folders. Future branch and editorial APIs remain explicitly separate.
- Old project versions must load without user edits. Older plugin binaries are not promised support for manifests saved in a new schema.
- Keep node inputs optional and compatible with saved widget restoration. Prefer explicit route operation targets; capture targets before queueing so later UI selection cannot redirect execution.
- Duplication must preserve folder hierarchy, branches, publication references and clip occurrences; remap rewritten archive hashes consistently. Reset-without-renders retains organization and edit definitions, marking unavailable clip sources clearly.
- Preserve normal generation, Stage 3A refinement, hires, references, identities, state anchors, templates, card LoRAs, preview, draft history and recovery throughout the rollout.

## Implementation map

| Area | Required work |
| --- | --- |
| `longcaster/project.py` | Additive migrations and validation; explicit operation targets; remove global-tail assumptions from branch operations. `organize_timeline()` and `organize_generation_folder()` remain separate organization entry points. |
| New generation organization/history module | Folder/branch validation, UUID indexes, source/publication lookup, dependency queries and impact calculations. Keep these pure where possible for CPU testing. |
| `longcaster/routes.py` | Explicit folder, branch, ancestry and editorial sequence endpoints with existing revision checks and project locking; summaries include shot labels and source-history availability. |
| `nodes.py` | Freeze the explicit target/source when scheduling; retain compatible defaults and one operation per project. |
| `longcaster/hires_project.py` | Publication-aware ancestry and historical derivative resolution; replace global-index/topological-order assumptions. |
| `longcaster/timeline.py`, `timeline_export.py` | Preserve legacy selection and add explicit clip-occurrence selection, publication lookup and ordered native/hires export. |
| `longcaster/duplicate.py` | Copy all organization/edit structures and retain correct publication/asset references after hash rewriting. |
| `web/longcaster.js` and focused UI modules | Separate Generations/Timelines navigation; extract folder browser/history/sequence controls rather than continuing to grow one workspace function. |

## Working at 200+ shots

Build card, publication, parent and children indexes once per loaded revision. Folder counts/search should not repeatedly traverse all ancestry. Render the visible folder/list portion and load preview thumbnails on demand; release video players for offscreen or deselected shots. Preserve selection and scroll position when state refreshes.

Keep the existing atomic manifest and project lock initially. Measure before introducing a database. A synthetic 250-shot fixture should include multiple scenes, alternative branches, several takes per card and hires metadata. Use it to check search, navigation, response payload size, DOM/media counts and graph expansion. Set measured UI budgets during implementation; no performance numbers are claimed by this plan.

## Verification and release gates

Use CPU tests and synthetic metadata/assets for schema migration, source resolution, cycle validation, branch adoption, scoped operations, reference retention, duplicate/reset/recovery and exact export selection. Test stale revisions and switching the selected folder/branch while a job is pending.

Frontend checks should cover legacy projects, empty/unassigned folders, bulk moves, folder cycles, collapsed-tree search results, branch source navigation, independent browse/job targets and independent sequence edits. Verify that organization changes do not mark draft render inputs dirty.

Maintain regressions for generation modes, card references, per-card LoRAs, identities/state anchors, color continuity, draft/take restoration, refinement source selection, hires ancestry, previews and legacy widget defaults. Editorial reordering must cause zero generation requests and leave fingerprints unchanged.

Manual visual/GPU validation remains user-controlled. No workflows were inspected or run. The next implementation slice is the remaining **Stage 1** browsing/history work, with the later stages gated as described above.
