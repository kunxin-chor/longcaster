# MiniMax H3 LongCaster MVP plan

The implementation follows `LONGCASTER_ARCHITECTURE.md`, with proof order adjusted for the production workflow:

1. **PDD + REF2VA contract first — implemented.** Accept a patched `MODEL`, require external PDD `SIGMAS` by default, resolve MMH3 references, and use REF2VA conditioning for every card in a fixed-mode project. Validate stock REF2VA + PDD first; current PDD calls the hybrid-merged trunk untested.
2. **Backend persistence proof — passed without GPU.** Save a synthetic H3 sampler-output MMH3, restart the Python process, load and verify it, prepare the 39-frame joint AV handover, and save the continued card.
3. **Project/card state — implemented and tested.** UUID cards, explicit ancestry, atomic manifest writes, cross-process project lock, immutable accepted archives, acceptance journals, and restart reconciliation.
4. **Controller operations — implemented and tested.** Generate, Retry, Accept, Append, Resume, duration alignment, generation fingerprints, and separate decode/preview.
5. **Minimal UI/workflows — implemented.** Controller buttons, PDD REF2VA example, and standard T2VA baseline.
6. **Joined timeline export — implemented.** Stream accepted cards in timeline order to NVENC while removing each continuation prefix from video and audio.
7. **Stage 2A current-state anchors — implemented; visual A/B pending.** Decode and persist an accepted card's final frame, attach it by source-card and anchor UUID, and apply it at the next card's frame-38 handover boundary through native `MiniMaxH3AddGuide` conditioning. Preserve the raw prompt while optionally adding the minimal authoritative-state instruction to the effective prompt.
8. **Production acceptance — requires local GPU run.** Run the documented cap/jersey four-card A/B, then generate and accept five PDD REF2VA cards, restart ComfyUI, resume, and generate Card 6 while checking state, identity/reference influence, and the AV seam.

Scope remains limited to a fixed generation mode, direct latent continuation, and one automatic last-frame state anchor. Manual/historical anchor selection, additional drift-control methods, and mixed card modes remain later work.
