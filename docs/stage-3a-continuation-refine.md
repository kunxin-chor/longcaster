# Stage 3A: continuation refine

Stage 3A adds a same-resolution second H3 sample as a rebuildable continuation derivative. It does not replace the accepted clip and does not implement CLSS, upscale, color correction, scene detection, periodic cadence, or audio recomposition.

## Runtime contract

The project node accepts a separate `refine_sigmas` input, an optional `refine_model`, a refine sampler, and an inherit/offset seed policy. One refine pass is run with the accepted card's reconstructed prompt, references, current-state guide, and identity reference. The default model source is the first-pass `MODEL`; no checkpoint is loaded internally.

The second sample operates on the joint H3 video/audio latent. For a continued card, LongCaster rebuilds the exact direct-MMH3 handover from that card's frozen continuation source, copies its hard-protected prefix values into an in-memory refine input, and attaches the reconstructed joint mask. Both video and audio protected regions must contain hard-locked values. After sampling, every hard-protected value must be bit-exact; otherwise the refine fails and no derivative is published. Card 1 has no inherited prefix, so the complete joint latent is eligible for refinement.

The initial schedule is deliberately graph-owned. LongCaster requires an explicit `refine_sigmas` connection and does not assume that the first-pass or PDD schedule is suitable. A useful experimental starting point from H3 Director is Euler with `(0.85, 0.7250, 0.4219, 0.0)`, but this is not hard-coded and must be verified against the connected H3/PDD model stack.

## Persistence and continuation selection

Acceptance completes first. Refine then creates `derivatives/<card UUID>/refine_<derivative UUID>.mmh3`, verifies the archive, rechecks the accepted source hash, and commits the derivative as `READY`. The accepted master is never opened for writing.

Each derivative records source IDs and hashes, cadence, model summary/source, sampler, exact SIGMAS and hash, steps, seed policy/value, resolution, conditioning policy, prefix-protection method/counts, joint-AV behavior, output artifact, timing, status, and error. An appended direct-continuation card freezes either the accepted master or a ready derivative in `continuation_source`.

Studio exposes synchronized **Off**, **Auto**, and **Manual** project radio controls, including the Subject tab. Off overrides every card setting and disables post-accept generation. Auto refines every card immediately after its master is accepted. Manual uses the editable card's **Refine enabled** checkbox at acceptance time. The checkbox is retained but ignored and disabled outside Manual mode, and becomes read-only after acceptance. Every accepted card shows **Generate Refine** or **Regenerate Refine** when refinement is not Off. A successful automatic or enabled-manual refine becomes the default continuation source; a failed regeneration preserves any earlier ready derivative.

Changing to Auto is prospective and does not refine historical accepted cards. Duplicate refine operations and unpublish are blocked while another project operation is pending. Unpublishing removes all refine files and metadata for that card, while retaining its immutable master publication. Failure or interruption records `FAILED` without reverting acceptance.

## Automated and production validation

Lightweight tests cover cadence/seed and Off/Auto/Manual precedence, per-card policy persistence, joint mask restoration, fail-closed video/audio prefix checks, immutable-master hashing, derivative state transitions, cancellation, regeneration fallback, unpublish cleanup, explicit selection, and frozen child lineage. They use synthetic data and do not execute ComfyUI workflows.

Production validation remains user-run: compare matched long sequences with refine Off, Auto, and Manual transition-only refine. Judge inherited health in later cards—not merely whether the derivative itself looks better—and record saturation, luminance, contrast, texture, prompt adherence, identity/state retention, video/audio seams, and generation time.
