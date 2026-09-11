# MiniMax H3 LongCaster MVP plan

The implementation follows `LONGCASTER_ARCHITECTURE.md`, with proof order adjusted for the production workflow:

1. **PDD + REF2VA contract first — implemented.** Accept a patched `MODEL`, require external PDD `SIGMAS` by default, resolve MMH3 references, and use REF2VA conditioning for every card in a fixed-mode project. Validate stock REF2VA + PDD first; current PDD calls the hybrid-merged trunk untested.
2. **Backend persistence proof — passed without GPU.** Save a synthetic H3 sampler-output MMH3, restart the Python process, load and verify it, prepare the 39-frame joint AV handover, and save the continued card.
3. **Project/card state — implemented and tested.** UUID cards, explicit ancestry, atomic manifest writes, cross-process project lock, immutable accepted archives, acceptance journals, and restart reconciliation.
4. **Controller operations — implemented and tested.** Generate, Retry, Accept, Append, Resume, duration alignment, generation fingerprints, and separate decode/preview.
5. **Minimal UI/workflows — implemented.** Controller buttons, PDD REF2VA example, and standard T2VA baseline.
6. **Joined timeline export — implemented.** Stream accepted cards in timeline order to NVENC while removing each continuation prefix from video and audio.
7. **Stage 2A current-state anchors — implemented; visual A/B pending.** Decode and persist an accepted card's final frame, attach it by source-card and anchor UUID, and apply it at the next card's frame-38 handover boundary through native `MiniMaxH3AddGuide` conditioning. Preserve the raw prompt while optionally adding the minimal authoritative-state instruction to the effective prompt.
8. **Stage 2B visual identity anchor — implemented; visual A/B pending.** Select a project and accepted card, scrub its registered preview, persist the exact MMH3 frame by UUID, and add the active subject checkpoint through native REF2VA `minimax_refs` while retaining the Stage 2A guide and direct latent prefix.
9. **Production acceptance — requires local GPU run.** Run the documented state and identity A/B sequences, then generate and accept five PDD REF2VA cards, restart ComfyUI, resume, and generate Card 6 while checking state, identity/reference influence, and the AV seam.
10. **Middle-card retake — investigated, not implemented.** Use reroll plus transitive descendant invalidation as the reliable baseline. An opt-in FL2VA bridge may preserve later rendered media by constraining the replacement between its neighbors, but those descendants remain ancestry-stale and require seam review. See `docs/fl2va-middle-retake.md`.

Scope remains limited to a fixed generation mode, direct latent continuation, one automatic last-frame state anchor, and one manually selected active identity anchor for the main subject. Automatic anchor scoring, CLSS, additional roles, mixed card modes, and middle-card retakes remain later work.
