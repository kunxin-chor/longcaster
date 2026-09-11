# ComfyUI MiniMax H3 LongCaster

LongCaster is a persistent card controller for iterative MiniMax H3 generation:

`Generate → Retry → Accept → Append → Continue`

Each accepted card is an immutable `.mmh3` archive. A project manifest records stable card IDs, prompts, requested and actual durations, seeds, ancestry, fingerprints, and the active card, so the chain can resume after ComfyUI restarts.

The current implementation gives early priority to the two production paths this repository targets:

- a pre-patched REF2VA model, with the REF2VA/FL2VA hybrid available as an explicit experimental comparison;
- PDD-ACC's patched `MODEL` and exact external `SIGMAS` schedule.

LongCaster never loads or unwraps the diffusion model. It sends the supplied `MODEL` to Comfy's Basic Guider unchanged. When `SIGMAS` is connected, the same tensor is passed to `SamplerCustomAdvanced`; LongCaster does not rebuild PDD's schedule.

All checkpoint selection stays in ordinary ComfyUI loader nodes. LongCaster contains no model filename or internal loader, so diffusion models, hybrid presets, text encoders, VAEs, and PDD files can be changed in the workflow UI.

## Installation

Install these custom nodes beside this repository in `ComfyUI/custom_nodes`:

- `einhorn13/mmh3_media` — required for reference packets, card archives, validation, and continuation handovers;
- `Jalen-Brunson/ComfyUI-MiniMax-H3-PDD-Acc` — required for the preferred PDD workflow;
- `Kosinkadink/ComfyUI-VideoHelperSuite` — required by the bundled NVENC preview output;
- `ComfyUI_MinimaxH3HybridLoader` — optional, for the patched REF2VA/FL2VA hybrid model;
- `tritant/ComfyUI_MiniMax_H3_Extender` — useful as a comparison workflow, but not a runtime dependency.

Update ComfyUI to a revision with native MiniMax H3 REF2VA conditioning and joint video/audio denoise-mask handling. Restart ComfyUI after installing the nodes. LongCaster finds a loaded `mmh3_media` package or a sibling custom-node directory containing `mmh3_media/__init__.py`.

No additional Python package is required by LongCaster itself.

## Preferred PDD + REF2VA workflow

Load [longcaster_pdd_ref2va.json](example_workflows/longcaster_pdd_ref2va.json). Replace its model filenames and `reference.png` with your assets.

The editable loader defaults use exact filenames from the official repositories:

| Loader | Default filename |
|---|---|
| REF2VA diffusion model | `minimax_h3_ref2va_int8_convrot.safetensors` |
| Standard FL2VA diffusion model | `minimax_h3_fl2va_int8_convrot.safetensors` |
| Text encoder | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` |
| Video VAE | `minimax_h3_video_vae_fp16.safetensors` |
| Audio VAE | `minimax_h3_audio_vae_fp32.safetensors` |
| REF2VA PDD-ACC | `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` |

These are workflow defaults only. Select any compatible installed alternative from the corresponding ComfyUI dropdown, or replace the model loader with the hybrid loader and connect its `MODEL` output.

Match the PDD head bank to the diffusion trunk that enters PDD Apply:

| Diffusion trunk entering PDD Apply | PDD-ACC file |
|---|---|
| stock REF2VA | `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` |
| stock FL2VA | `MiniMax-H3-FL2VA-Acc-8Step.safetensors` |
| FL2VA with the reference patch | `MiniMax-H3-FL2VA-Acc-8Step.safetensors` |
| merged REF2VA/FL2VA hybrid | experimental; establish a same-trunk baseline before testing |

The reference patch adds reference conditioning but does not turn an FL2VA diffusion trunk into the stock REF2VA trunk expected by the REF2VA PDD heads. Keep `partition_check=error`. A family mismatch can run when that check is reduced to `warn`, but its output may be visibly degraded.

The critical connections are:

```text
stock REF2VA MODEL
  → MiniMax H3 Sigma Shift (video 12, audio 3)
  → PDD Apply
  → Model Attention Backend (comfy kitchen attention)
  → LongCaster MODEL

PDD Apply SIGMAS
  → LongCaster SIGMAS

MMH3 reference packet
  → LongCaster reference_packet
```

Keep `sampler_name=euler`, `require_external_sigmas=true`, and `generation_mode=ref2va`. The reference packet is resolved for every generation, including later direct-continuation cards. Use native tags such as `<Picture 1>`, `<Video 1>`, and `<Audio 1>` in each card prompt.

With external PDD `SIGMAS` connected, LongCaster's `steps` and `scheduler` widgets are intentionally ignored; PDD Apply owns the number of evaluations and the exact schedule. `sampler_name` remains active and must be `euler`. The example pins PDD's `partition_check=error` so a Ref2VA/FL2VA trunk mismatch stops instead of producing a degraded render.

The examples use Video Helper Suite with LongCaster's `video/longcaster_nvenc_h264-mp4.json` profile. It matches the established exporter settings: `h264_nvenc`, preset `p4`, HQ tune, VBR rate control, CQ 17, unrestricted peak bitrate, YUV 4:2:0, and AAC at 192 kbps. CQ and preset remain editable in the Video Combine node. Replace it with ComfyUI's native Save Video if NVENC or Video Helper Suite is unavailable.

The PDD REF2VA example selects `comfy kitchen attention` explicitly to match the established comparison workflow for reproducibility. It is not expected to provide a material quality improvement by itself.

The current PDD-ACC source explicitly describes PDD on a hybrid-merged trunk as **untested**. Assess the combinations in this order:

1. stock REF2VA + references + PDD MODEL/SIGMAS (supported PDD baseline);
2. hybrid MODEL + the same references with standard sampling (reference-influence baseline);
3. hybrid + PDD MODEL/SIGMAS only as an experimental combination.

LongCaster remains patch-agnostic and will accept the hybrid output, but it cannot establish that the PDD head bank is valid for merged hybrid weights. PDD's own partition, shift, and sampling guards remain authoritative.

For gate 2, keep the reference-packet side of the PDD example, connect the Hybrid Loader `MODEL` directly to LongCaster, disconnect `SIGMAS`, and set `require_external_sigmas=false` with `generation_mode=ref2va`. For gate 3, route that hybrid model through Sigma Shift and PDD Apply again and restore the exact `MODEL`/`SIGMAS` connections.

The example uses `MMH3Create → MMH3Put` for one image. Add more `MMH3Put` nodes for image, video, and audio resources. For a large reusable reference library, save that packet with `MMH3Save` and reload it with `MMH3Load`; reconnect the same packet when resuming the LongCaster project.

## Standard sampling baseline

Load [longcaster_standard_t2va.json](example_workflows/longcaster_standard_t2va.json). It connects an ordinary H3 `MODEL`, leaves `SIGMAS` disconnected, and sets `require_external_sigmas=false`. LongCaster then builds the selected standard Comfy sigma schedule. The project remains fixed to T2VA.

## Card controls

The frontend extension adds six buttons to the main node. The `action` widget remains available for queued or API workflows.

1. **Resume Project** creates the project if necessary or reloads `project.json`. An interrupted generation is cleared safely; accepted cards remain unchanged.
2. **Stop Render / Unlock** requests ComfyUI's normal render interrupt, then clears this project's pending-generation marker. An active `DRAFT` remains unchanged, and a late result from the cancelled operation cannot replace it.
3. **Generate Draft** changes the active `EMPTY` card to `DRAFT` after sampling and full MMH3 verification.
4. **Retry Draft** regenerates only the active draft. It reads the same accepted parent and permits a changed prompt, duration, or seed.
5. **Accept Draft** copies the verified draft to a new immutable master and commits the manifest through an acceptance journal.
6. **Append Card** creates an `EMPTY` child of the current accepted card. Its prompt starts blank; enter its own prompt, duration, and seed before generating.

Project mode and canvas size are fixed at creation. This prevents accidental per-card mixing. Use a new project name to change between `ref2va` and `t2va` or to change resolution.

Requested duration means new timeline duration. H3 output is aligned to the nearest `17k+5` frame count. A continuation reserves 39 pixel frames as the direct latent handover, so both requested duration and actual new/generated frame counts are recorded.

## Stage 2A current-state anchors

Accepting a card now decodes its final video frame and writes a lossless PNG current-state anchor beside the project. The accepted `.mmh3` master remains immutable. For every later card, LongCaster still creates the direct joint AV latent handover and also feeds the source card's anchor through ComfyUI's native `MiniMaxH3AddGuide` / `minimax_keyframes` conditioning.

Because a continuation target starts with the accepted parent's 39 preserved frames, the parent's final frame is anchored at target frame 38. The anchor therefore lines up with the end of the preserved prefix and the boundary into newly generated frames. Persistent Ref2VA references remain active for identity, while the current-state anchor represents the latest observable clothing, hair, held objects, injuries, and accessories.

The main node has two compatibility controls:

- `auto_state_anchor=true` activates the accepted parent's current-state anchor. Disable it to run the latent-only control.
- `reinforce_state_prompt=true` adds one authoritative-state instruction when an anchor is active. It inserts the instruction into `summary` and `retention_analysis` sections when present; otherwise it appends it without rewriting the supplied prompt. The raw prompt and effective prompt are both recorded.

Anchor assets are stored under:

```text
ComfyUI/output/longcaster_projects/<project_name>/anchors/<source_card_uuid>/<anchor_uuid>.png
```

Projects created before Stage 2A migrate automatically when resumed. Their existing accepted masters are unchanged. If an accepted parent has no anchor, LongCaster extracts and records it from that master's final decoded frame immediately before the next generation.

The normal draft preview caches its already-decoded final frame, so **Accept Draft** can commit the anchor without loading either VAE again. If a custom workflow never decodes the draft preview, acceptance falls back to a video VAE decode so the continuity anchor is still guaranteed. Accept, Append, and Stop/Unlock block their media outputs to prevent the connected preview exporter from rerunning. Console diagnostics report the source card UUID, decoded source frame and timestamp, asset path, native mechanism, target frame, anchor-active state, and prompt-reinforcement state.

### Cap and jersey continuity test

Use a new REF2VA project so the original reference shows the character wearing both a cap and jersey. Keep the same reference packet and PDD wiring throughout.

1. Card 1: prompt the character wearing the cap and jersey. Generate and accept it.
2. Card 2: prompt the character removing the cap and ending without it. Generate and accept it.
3. Card 3: prompt the character removing the jersey and ending shirtless without the cap. Generate and accept it.
4. Append Card 4. Use an action-only prompt that does not mention the cap, jersey, or shirtless state, such as `The character walks toward the doorway as the camera follows.` Keep its seed fixed.
5. For the latent-only control, set `auto_state_anchor=false` and `reinforce_state_prompt=false`, then Generate Draft and keep its preview.
6. Restore `auto_state_anchor=true` and `reinforce_state_prompt=true`, keep the same Card 4 prompt and seed, then Retry Draft.
7. Compare the two Card 4 previews. The retry is the Stage 2A result: direct continuation from accepted Card 3 plus Card 3's final-frame current-state guide. Confirm the console reports Card 3's UUID, its final decoded frame/timestamp, the PNG path, `MiniMaxH3AddGuide/minimax_keyframes`, target frame 38, and prompt reinforcement enabled.

This A/B test measures whether the native state anchor reduces restoration of the older cap-and-jersey appearance. It does not claim perfect state preservation; H3 can still override conditioning, especially when the persistent identity reference strongly depicts the older state.

Projects are stored under:

```text
ComfyUI/output/longcaster_projects/<project_name>/
```

The separate **MiniMax H3 LongCaster Decode** node decodes the packet for preview. With `trim_context=true`, it removes the repeated 39-frame continuation prefix from both video and audio preview outputs. Decoding does not participate in continuation or persistence.

The example's NVENC card previews are written under `ComfyUI/output/video/`. These are individual active-card previews. Accepted card masters remain under the project `clips/` directory.

## Joined timeline export

The PDD REF2VA example includes **MiniMax H3 LongCaster Timeline Export**. It reads accepted `.mmh3` masters in timeline order, decodes one card at a time, removes the recorded continuation prefix from every continuation card, and streams the retained frames to NVENC. This keeps the complete decoded timeline out of RAM.

To test a long clip:

1. Generate and accept Card 1.
2. Append, generate, and accept at least Card 2. Repeat for more cards.
3. Leave the main LongCaster node on `resume`.
4. Set the export node's `project_name` to the same project, turn `enabled=true`, and Queue Prompt.
5. Open `ComfyUI/output/video/longcaster_joined_#####_.mp4` or use the preview shown on the export node.

By default, only immutable accepted cards are included. Turn on `include_active_draft` to append the current draft for a temporary continuity review. The joined exporter uses H.264 NVENC with editable preset and CQ controls plus AAC audio; its defaults are `p4`, CQ 17, and 192 kbps. Set `enabled=false` again after export so ordinary card operations do not rebuild the timeline.

## Restart and resume test

Run this sequence with the PDD example:

1. Generate, inspect, and Accept Card 1.
2. Append, enter Card 2 settings, Generate, and Accept.
3. Repeat through Card 5 and confirm `clips/card_0001.mmh3` through `card_0005.mmh3` exist.
4. Close ComfyUI completely and restart it.
5. Reload the workflow and the same reference packet, then click Resume Project.
6. Append and generate Card 6.

Accepted archives are checked against the SHA-256 stored in `project.json`. LongCaster refuses to overwrite an existing accepted filename.

## Tests

The standard-library suite does not import ComfyUI or require a GPU:

```powershell
python -m unittest discover -s tests -v
```

The real MMH3 backend smoke uses ComfyUI's Python and the MMH3 repository path. It writes a synthetic H3 latent, saves it, launches a fresh process, reloads it, builds a direct continuation, and saves the next card:

```powershell
E:\path\to\python_embeded\python.exe tests\mmh3_backend_smoke.py --mmh3-root E:\path\to\ComfyUI_mmh3_media
```

See [project format](docs/project-format.md), [implementation notes](docs/implementation-notes.md), and the detailed [architecture report](LONGCASTER_ARCHITECTURE.md).

## MVP boundaries

The controller supports one linear active tail, one generation mode per project, and the automatic final-frame current-state anchor. Branching, card-mode switching, user-selected guides, re-encoded history, manual/historical anchor selection, CLSS, additional anti-drift methods, latent upscale, bridge retakes, prompt composition, and custom decoder controls remain deferred. Reference resources remain in their own MMH3 packet and must be reconnected for future REF2VA generations after restart.
