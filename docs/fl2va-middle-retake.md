# FL2VA middle-card retake investigation

Status: design investigation only. This document does not change LongCaster's current tail-only generation behavior.

## Question

Can LongCaster replace a card in the middle of an accepted sequence with FL2VA while keeping the later accepted cards?

Yes, as an experimental visual bridge. It cannot make the existing later cards true latent continuations of the replacement. The product must preserve that distinction in its ancestry and UI.

## Relevant native behavior

MiniMax H3 Base FL2VA accepts a first frame, a last frame, or both. ComfyUI's native `MiniMaxH3ImageToVideo` encodes those images as `minimax_keyframes` at frame 0 and the final generated frame. `MiniMaxH3AddGuide` also places an image or short media guide at a selected frame index. These are the native mechanisms LongCaster should use; no new embedding format is needed.

The installed Extender 2.0 treats FL2VA cards as independent plans and can replace a plan without rewriting unrelated caches. Its apparent random access has an important qualification: a card whose first frame came from `previous_clip` stores the predecessor clip ID and frame signature. Changing that predecessor invalidates dependent cached cards transitively. Independent manual endpoints survive; chained motion ancestry does not.

The installed `mmh3_media` chain provides the safer semantic model. `Reroll accepted` proves that the supplied source is the target card's recorded parent, archives the old revision, and marks every later active segment invalidated. It never relabels old descendants as if they had been generated from the replacement.

## Two valid retake modes

### 1. Reroll and invalidate descendants

Regenerate card N from the accepted MMH3 master of card N-1, using the existing 39-frame joint audio/video latent continuation, references, anchors, prompt, patched model, and external PDD sigmas. Archive the prior publication of card N and mark N+1 onward stale. Then regenerate descendants in order.

This is the robust default because it preserves real generation ancestry, motion context, and audio context. It costs more render time.

### 2. FL2VA bridge and preserve downstream media

Construct a replacement card between fixed visible boundaries:

- first boundary: the final visible frame of accepted card N-1;
- last boundary: the first visible frame of accepted card N+1, after N+1's stored continuation prefix;
- middle: the new card prompt plus the project's applicable identity/reference conditioning;
- output: an independent FL2VA replacement for card N.

This can make the assembled timeline meet both neighboring shots visually without rerendering N+1 onward. It works best at a cut, a held pose, or a low-motion seam.

The preserved successor remains ancestry-stale. Its first visible frame can match the new bridge, but its hidden 39-frame prefix, motion trajectory, audio state, and generation history still came from the old card N. LongCaster must not rewrite `generation_parent_id` to conceal this.

## Recommended experimental variant

For the current LongCaster workflow, first test a constrained continuation rather than a purely independent FL2VA clip:

1. Build the normal direct 39-frame joint AV continuation from card N-1.
2. Add the successor's first visible frame as a native H3 image guide at the replacement's final visible frame.
3. Keep persistent Ref2VA references and the selected identity anchor active when the patched FL2VA/reference model supports their combination.
4. Generate with the FL2VA PDD model family and its matching external sigmas.

This variant retains exact incoming latent motion/audio context and constrains the outgoing image. It also fits LongCaster's current duration calculation. The combination of a continuation latent, an end guide, and Ref2VA conditioning is model-patch dependent and therefore needs an A/B GPU test before it becomes a supported mode.

## Duration constraint

H3 independently generated video lengths lie on the `17k + 5` frame grid. A continuation card can have a different visible length because LongCaster generates a legal total length and removes the 39-frame handover prefix.

For example, a 277-frame continuation contains 39 context frames and 238 visible frames. A purely independent FL2VA replacement cannot naturally generate exactly 238 frames on the `17k + 5` grid; the nearest useful legal length is 243. Trimming or accepting a five-frame timing change can weaken or remove the final-frame endpoint constraint.

The direct-prefix plus end-guide variant avoids that mismatch: generate the same 277 total frames, trim the same 39-frame prefix, and retain 238 visible replacement frames.

## Proposed persistence contract

Use stable card and publication UUIDs. Keep the accepted MMH3 files immutable.

Add version and validity metadata rather than changing historical ancestry:

- `publication_id`: identity of the selected card version;
- `supersedes_publication_id`: replaced version of the same logical card;
- `generation_parent_id` and `generation_parent_publication_id`: actual latent source used for generation;
- `boundary_constraints`: left/right source card ID, publication ID, frame index, timestamp, asset path, and native conditioning mode;
- `downstream_validity`: `valid`, `stale`, or `seam_unverified`;
- `invalidated_by_publication_id`: replacement that made a descendant stale;
- `assembly_predecessor_publication_id`: selected predecessor for the assembled timeline, kept separate from generation ancestry;
- seam review result and reviewer timestamp for an experimentally preserved successor.

Existing `publication_history` can hold superseded versions. A future card UI should expose the current version and its history instead of deleting or overwriting either artifact.

## State transition proposal

For **Reroll and invalidate descendants**:

1. Lock the project and verify that card N is accepted and card N-1 is its recorded accepted parent.
2. Create a replacement draft without modifying the accepted version.
3. On acceptance, move the old publication into history and select the new publication.
4. Mark every transitive descendant stale while retaining its immutable assets.
5. Exclude stale descendants from the normal joined export until regenerated or explicitly placed on a branch.

For **FL2VA bridge and keep downstream**:

1. Snapshot both neighbor publication IDs and boundary-frame hashes before generation.
2. Generate and review the replacement as a draft.
3. On acceptance, archive the old target publication and mark preserved descendants `seam_unverified`.
4. Require explicit seam review before the normal exporter treats the preserved path as publishable.
5. If either boundary publication changes, invalidate the bridge automatically.

## Audio and motion limitations

Two matching images do not specify velocity. FL2VA may enter the successor with a different body direction, camera movement, water motion, lighting evolution, or object trajectory. A swimming sequence is a difficult case because water and limb motion expose discontinuities quickly.

The successor's audio was conditioned on the old latent parent. Keeping it may produce a click, ambience jump, interrupted speech, or semantic mismatch. The bridge mode should initially be restricted to cards without continuous dialogue across either seam, or require separate audio seam review and crossfade policy.

Identity references help facial consistency but cannot guarantee pose, expression, wardrobe state, or motion compatibility. The authoritative current-state anchor should describe the incoming visible state; the outgoing boundary frame constrains the state required by the preserved successor.

## UI implication

The future all-cards interface should offer two explicit actions on an accepted middle card:

- **Retake; regenerate following cards** — supported and reliable;
- **FL2VA bridge; keep following renders** — experimental and followed by seam review.

Before queueing, show which publications will become stale, which will be retained, and whether the requested visible duration fits a native FL2VA length. The timeline should display stale and seam-unverified cards distinctly.

## Validation experiment

Build one five-card project and retain all seeds, prompts, models, patch strengths, PDD sigmas, and accepted masters.

1. Retake card 3 with normal direct continuation and regenerate cards 4–5. Treat this as the quality baseline.
2. Restore the original branch and replace card 3 with a pure first/last-frame FL2VA bridge while preserving cards 4–5.
3. Replace card 3 again using direct continuation from card 2 plus a native end guide from card 4's first visible frame.
4. Run all three variants on a low-motion or shot-boundary sequence.
5. Repeat on the swimming sequence, where card 3 changes the subject's action or appearance before card 4.
6. Compare the two seams frame by frame and in motion: identity, clothing/state, pose, movement direction and speed, camera motion, water continuity, lighting, audio waveform/clicks, exact visible duration, and duplicated boundary frames.
7. Reject bridge mode as a supported feature if the continuous-motion case only hides the discontinuity in still-frame inspection.

## Recommendation

Implement general middle-card reroll with descendant invalidation first. It is useful for every model mode and matches the project's immutable ancestry rules. Add the FL2VA bridge as an opt-in experiment after that foundation exists. For the first bridge prototype, use the existing direct latent prefix from the left neighbor and a native final-image guide from the right neighbor; it has the best chance of preserving incoming motion and exact duration in LongCaster's current architecture.
