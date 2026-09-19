# Stage 3A: continuation refine

Stage 3A adds a same-resolution second H3 sample as a rebuildable continuation derivative. It does not replace the accepted clip and does not implement CLSS, upscale, color correction, scene detection, periodic cadence, or audio recomposition.

## Runtime contract

The project node accepts a separate `refine_sigmas` input, an optional `refine_model`, a refine sampler, and an inherit/offset seed policy. One refine pass is run with the accepted card's reconstructed prompt, references, current-state guide, and identity reference. The default model source is the first-pass `MODEL`; no checkpoint is loaded internally.

The second sample operates on the joint H3 video/audio latent. For a continued card, LongCaster rebuilds the exact direct-MMH3 handover from that card's frozen continuation source, copies its hard-protected prefix values into an in-memory refine input, and attaches the reconstructed joint mask. Both video and audio protected regions must contain hard-locked values. After sampling, every hard-protected value must be bit-exact; otherwise the refine fails and no derivative is published. Card 1 has no inherited prefix, so the complete joint latent is eligible for refinement.

The initial schedule is deliberately graph-owned. LongCaster requires an explicit `refine_sigmas` connection and does not assume that the first-pass or PDD schedule is suitable. A useful experimental starting point from H3 Director is Euler with `(0.85, 0.7250, 0.4219, 0.0)`, but this is not hard-coded and must be verified against the connected H3/PDD model stack.

## Card-local Ref2VA reference integration

Implementation status: the one-image MVP, Studio controls, schema/take lifecycle, native latent substitution, conservative in-memory cache, and accepted-source refine reconstruction are implemented. CPU tensor/persistence and frontend-control tests cover the feature; installed ComfyUI and real-model acceptance remain pending. Studio shows the last graph-resolved picture number, refreshed when the packet executes.

Card-local Ref2VA image references are a separate, cross-cutting authoring feature rather than part of the refine algorithm. Their base implementation must cover card-owned assets and metadata, Studio controls, native reference assembly, latent softening and caching, generation fingerprints, take provenance, migration, and cleanup. Stage 3A depends only on the integration contract below and must not duplicate that machinery.

For every generation path, the effective native image order is deterministic:

1. Images from the connected project MMH3 reference packet.
2. Enabled card-local image references.
3. Enabled LongCaster identity-anchor images, ordered by subject ID.

Video and audio references retain native MiniMax ordering after those images. Card and identity references are exposed to MiniMax as ordinary `<Picture N>` images; their indexes are calculated from the effective reference list and are never represented by aliases or prompt rewrites. The native image-count limit includes project images, the card image, and every enabled subject identity.

Latent softening, when enabled, changes only the card picture's native VAE reference latent. MiniMax's tokenizer/Qwen path continues to receive the original resized RGB image. A clean encoded latent `z` is reused and the effective latent is `strength * z`, where `1.0` is native Ref2VA behavior and `0.0` is a zero latent. The implementation replaces only the targeted cloned `minimax_refs` block and does not mutate shared conditioning, alter other references, or add a sampler pass.

Normal generation, Retry, and continuation generation build the effective reference set from the current card inputs and freeze them into the new take. Refine is stricter: it reconstructs the card reference image, resolved picture index, sizing, latent mode, strength, and cache/version metadata frozen by the accepted source card. It must not read mutable settings from a child card or silently attach current card metadata to an older artifact. Project-packet changes may move the live picture index and dirty a draft, but they cannot change immutable take or accepted-card provenance.

Card references are authoring inputs. Render invalidation, retry, unpublish, and refine-derivative cleanup retain them; removing a card removes its owned assets, and both full duplication and definition duplication copy them. T2VA disables and rejects card references. Accepted-card controls follow the existing read-only/editability rules.

Recommended sequencing is:

1. Land the base card-reference schema, asset lifecycle, native ordering, generation/take provenance, and normal Ref2VA behavior with no-reference backward compatibility.
2. Add deterministic latent substitution and conservative clean-latent caching keyed by source hash, native resized dimensions, sizing mode and canvas context, VAE identity, and encoding version. Strength changes reuse the clean latent.
3. Wire the same frozen reconstruction into Retry, continuation, and Stage 3A refine.
4. Add Studio controls and resolved `<Picture N>` feedback, then complete migration, lifecycle, frontend, and model-backed validation.

## Persistence and continuation selection

Acceptance completes first. Refine then creates `derivatives/<card UUID>/refine_<derivative UUID>.mmh3`, verifies the archive, rechecks the accepted source hash, and commits the derivative as `READY`. The accepted master is never opened for writing.

Each derivative records source IDs and hashes, cadence, model summary/source, sampler, exact SIGMAS and hash, steps, seed policy/value, resolution, conditioning policy, prefix-protection method/counts, joint-AV behavior, output artifact, timing, status, and error. An appended direct-continuation card freezes either the accepted master or a ready derivative in `continuation_source`.

Studio exposes synchronized **Off**, **Auto**, and **Manual** project controls under **Optional source refine**, including the Subject tab. Off overrides every card setting and disables post-accept refine generation. Auto refines every card immediately after its master is accepted. Manual uses the editable card's **Create refined source after accepting this card** checkbox at acceptance time. The checkbox is retained but ignored and disabled outside Manual mode, and becomes read-only after acceptance. Every accepted card shows **Generate Refine** or **Regenerate Refine** when refinement is not Off. A successful automatic or enabled-manual refine becomes the default continuation source; a failed regeneration preserves any earlier ready derivative.

Continuation refine and continuation treatment are orthogonal. Refine optionally determines which immutable source archive supplies the next direct-MMH3 handover: the accepted master or a ready refine derivative. **Drift-Control AV** and **Drift + Color-Stable AV** then apply their sampling policy to that resolved source. Selecting Off, Auto, or Manual therefore never disables or replaces the project-level continuation treatment.

Changing to Auto is prospective and does not refine historical accepted cards. Duplicate refine operations and unpublish are blocked while another project operation is pending. Unpublishing removes all refine files and metadata for that card, while retaining its immutable master publication. Failure or interruption records `FAILED` without reverting acceptance.

## Automated and production validation

Lightweight tests cover cadence/seed and Off/Auto/Manual precedence, per-card policy persistence, joint mask restoration, fail-closed video/audio prefix checks, immutable-master hashing, derivative state transitions, cancellation, regeneration fallback, unpublish cleanup, explicit selection, and frozen child lineage. With card-local references enabled, they also verify native ordering, resolved picture numbering, image-count enforcement, exact latent scaling, isolation of the targeted reference block, preservation of the tokenizer RGB input, dirty-state and take restoration behavior, cache reuse/invalidation, T2VA rejection, and reconstruction from the accepted source card during refine. They use synthetic data and do not execute ComfyUI workflows.

Production validation remains user-run: compare matched long sequences with refine Off, Auto, and Manual transition-only refine. Include runs with normal and softened card references and confirm the intended picture is used without weakening its multimodal/tokenizer presentation. Judge inherited health in later cards—not merely whether the derivative itself looks better—and record saturation, luminance, contrast, texture, prompt adherence, identity/state retention, pose/composition adherence, video/audio seams, and generation time. Real VAE cache compatibility and end-to-end conditioning behavior require a connected MiniMax H3 model stack.
