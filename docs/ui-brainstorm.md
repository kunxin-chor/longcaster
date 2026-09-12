# LongCaster UI and workflow brainstorm

Status: exploratory notes except for the Stage 2C Cards Interface + Structured Prompt Editor MVP now committed in `stage-2c-cards-interface.md`. That document controls Stage 2C scope when these ideas differ.

## Render and preview behavior

- Decouple **Generate Draft** from explicit joined-video export. They can currently execute together, causing unexpected work and model loading.
- A new draft should nevertheless refresh the continuously viewable project result. This creates an intentional design question: distinguish a lightweight or incremental preview refresh from the explicit final-quality stitched export.
- Provide continuous timeline playback without requiring creation of a new stitched MP4. Possible directions include a playlist/manifest player, sequential card playback, or a lightweight cached preview timeline.
- Provide direct access to the preview for every individual card and take, including earlier accepted cards and the active draft.
- Keep expensive output actions explicit and make their active/disabled state obvious.

## Reference and starting-media input

- Replace the current multi-node packet-building workflow with one guided, difficult-to-misconfigure node for:
  - image references;
  - audio and voice references;
  - video references and their paired audio;
  - starting image/video inputs;
  - first/last-frame guides where supported;
  - reference purpose, subject association, ordering, and enable/disable state.
- The node should explain native H3 ordinals. Picture, audio, and video numbering use separate namespaces, so image order 0 becomes `<Picture 1>` while audio order 0 becomes `<Audio 1>`.
- Validate incompatible modes, missing resources, duplicate ordering, unsupported counts, and model-family mismatches before queuing generation.
- Preserve an advanced path for direct MMH3 packet input and editing.

## Safer card controls

- Merge **Generate Draft** and **Retry Draft** into one context-aware action if this can be done without hiding destructive behavior. An `EMPTY` card would generate; a `DRAFT` card would create a new attempt while retaining the prior take until the replacement commits.
- Allow cards to be appended for story planning before the current draft is accepted. Planned card order must remain separate from accepted render ancestry so an unaccepted take cannot silently become continuation authority.
- Consider a storyboard mode where a crude storyboard image can condition generation for that card.
- Show validation and the action that will occur before work starts. Controls that cannot legally run in the current state should be disabled.
- Keep generation, project mutation, preview refresh, and final export as distinct backend operations even if the UI presents a simpler combined action.

## Storyboard and timeline questions

- Build an all-cards interface that shows every planned, draft, accepted, and invalidated version in timeline order. Each card should expose actions according to its state rather than relying on one global active-card control.
- The implemented tail-only **Unpublish Latest Card** is the first versioning primitive: it archives the immutable publication and reopens the live card as a draft. The future card interface should display this publication history and allow reviewing or restoring old takes.

- Define a planned-card state independent of `EMPTY`, `DRAFT`, and `ACCEPTED`, or add a separate storyboard record rather than weakening accepted-card semantics.
- Decide whether storyboard images are native H3 guides, references, starting frames, or planning-only assets. This may depend on the selected generation mode.
- Decide whether generating a draft automatically updates only a preview timeline, while final stitched export remains manual.
- When a middle card is regenerated, default to explicit downstream invalidation: retain descendant masters as historical versions, mark them stale because their generation ancestry points to the superseded parent, and require deliberate regeneration or branch selection. Never present old descendants as continuous with the replacement.
- Consider branching as an advanced alternative to downstream invalidation. An experimental FL2VA bridge can constrain a replacement between its neighbors while keeping later media, but it does not repair the later cards' latent ancestry. Keep them seam-unverified until reviewed. See `fl2va-middle-retake.md`.

## Card inspection and anchors

- Each card should expose its generated video, accepted master, draft attempts, prompt, seed, duration, and status from one card-oriented view.
- Show the automatic current-state anchor and manual identity anchor associated with a card.
- Anchor images should be toggleable so they do not consume space when the user is focused on motion or timing.
- Make it clear which asset currently controls temporal continuation, current visible state, and facial identity.

## Project management

- Add a server-side project picker based on validated project manifests rather than free-form path entry.
- Add an explicit **New Project** flow with name availability checking and initial mode/canvas/reference validation.
- Make project creation distinct from Resume so selecting the wrong project name cannot silently initialize an unintended project.
- Surface project mode, resolution, active card, accepted-card count, pending operation, and active anchors in the picker.
- Preserve project UUID/card UUID identity internally even when the UI displays friendly names and card numbers.

## Resolution controls

- Include the reusable resolution switcher in the standard LongCaster workflow/UI so it does not have to be manually inserted each time.
- Validate that the selected dimensions satisfy H3 canvas constraints before queueing.
- Because project resolution is currently fixed at initialization, changing resolution should either require a new project or become an explicit future migration/render-variant feature.

## Character and subject library

- Consider a server-side character/subject database containing:
  - subject ID and display name;
  - canonical face and body images;
  - wardrobe/reference sets;
  - voice and audio references;
  - generated identity checkpoints;
  - optional RefMods;
  - tags, provenance, and compatibility metadata.
- A project should bind a subject record by stable ID and snapshot the exact resources used for reproducibility.
- Keep character identity, current story state, and per-shot pose/composition separate in both storage and UI.

## RefMod support

- Investigate future support for MiniMax H3 RefMods using the linked [MiniMax-H3 RefMod Creation and Extraction Guide](https://huggingface.co/datasets/malcolmrey/various/blob/main/h3-center/docs/MINIMAX_H3_REFMOD_CREATION_GUIDE.md).
- The guide describes RefMods as compact, pre-encoded H3 VAE conditioning latents created from an image set and applied through `MiniMaxH3RefModsLoader` and `MiniMaxH3RefModApply`.
- Potential value for LongCaster includes faster reusable subject loading, fewer repeated VAE encodes, and better scaling to larger subject libraries.
- Before integration, verify compatibility with patched REF2VA/FL2VA models, PDD-ACC, native references, current-state guides, identity anchors, and the installed H3 conditioning implementation.
- RefMods should fit the same subject/reference abstraction rather than introducing a separate project workflow.

## Alternative generation backends

- Consider OpenVDN and FastH3 as additional generation backends while retaining LongCaster projects, cards, references, anchors, and timeline semantics.
- Verify the exact OpenVDN project/name and its available continuation contracts before design work.
- Backend-specific model inputs and sampling controls should be adapters behind the shared card/project model.
- Do not assume MMH3 latent continuation or H3 guide semantics transfer directly to another backend.

## Error-prevention principles

- Prefer constrained selectors populated from server state over free-form strings where practical.
- Disable illegal actions based on project/card state.
- Show the concrete effect of an action: generate, replace draft attempt, accept, append planning card, refresh preview, or export final timeline.
- Never let preview/export side effects trigger generation or project mutation.
- Never let planning assets or draft takes silently become accepted continuation sources.
- Validate references, ordinal mappings, canvas, model family, PDD schedule, and required inputs before loading large models.
- Keep accepted MMH3 masters immutable and make derived previews safe to rebuild.

## Unresolved design choices

- What should “stitch after every draft” produce: a temporary preview, an incremental cached timeline, or the final export format?
- Should a continuous player read card previews directly or request decoded frames from accepted MMH3 masters?
- How should planned storyboard cards relate to the current linear accepted-card ancestry?
- Should intermediate-card regeneration create a branch, invalidate later cards, or remain unsupported?
- How should a single reference-entry UI expose advanced MMH3 roles without recreating the current wiring complexity inside one large node?
- Which subject-library data belongs globally on the server and which must be snapshotted into each project?
- How should RefMods, raw native references, generated identity anchors, and current-state anchors be prioritized and diagnosed together?
