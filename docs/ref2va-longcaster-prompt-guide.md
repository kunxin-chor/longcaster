# MiniMax H3 Ref2VA + LongCaster Rewrite Output Format Guide

Use this guide to rewrite user requests for MiniMax H3 in Ref2VA full-reference mode, including independent LongCaster cards and direct continuations of accepted cards.

Write all six output sections in English. Preserve the original language only for dialogue and lyrics inside `<d>` and for text visibly present in the scene.

Make `detailed_description` as detailed and explicit as possible. For every shot, clearly establish the composition, referenced subjects, subject appearance and position, environment and lighting, actions and state changes, camera movement, current sound, and the points where referenced content appears or takes effect. Do not reduce it to a plot summary or a list of reference relationships.

## 1. Required Output Structure

Produce exactly these six sections, in this order:

```text
subject_definitions:
...

summary:
...

retention_analysis:
...

detailed_description:
...

overall_soundscape:
...

non_diegetic_music:
...
```

Use these plain text headings exactly. MiniMax H3 does not use XML for this format: do not wrap section names in angle-bracket opening or closing tags.

Do not add an image-alignment instruction before these sections. Do not use first-frame, first-and-last-frame, or last-frame alignment syntax unless a reference image genuinely serves as a concrete keyframe in addition to its Ref2VA role.

### 1.1 LongCaster runtime continuity additions

Return only the six authored sections. Do not add LongCaster's internal anchor instructions yourself and do not invent a reference label for an internal anchor.

When a card uses **Continue previous card (direct MMH3)**, LongCaster supplies the final 39 joint video/audio latent frames from the accepted parent as protected context. At 24 fps this is 1.625 seconds of hidden handover context. LongCaster removes those repeated frames from normal visible playback and timeline export. The requested card duration therefore means newly visible timeline duration.

When `auto_state_anchor=true`, LongCaster also applies the accepted parent's final decoded frame at handover frame 38 through its native keyframe-guide path. When `reinforce_state_prompt=true`, it creates a runtime-only `effective_prompt` and automatically inserts this exact sentence into `summary` and `retention_analysis`:

```text
Continue <Subject 1> from the current visual state shown in the continuation anchor. This state is authoritative where it conflicts with earlier reference appearance.
```

This resolves a deliberate priority rule: persistent Ref2VA assets remain the long-term reference, while the latest accepted final-frame state controls changed clothing, wetness, hair condition, carried objects, injuries, accessories, lighting-dependent appearance, and other visible state at the continuation boundary.

When `use_identity_anchor=true`, an enabled identity checkpoint is added after the user-provided image references as another native Ref2VA image. If `reinforce_state_prompt=true` too, LongCaster inserts one scope instruction at the start of `retention_analysis`, before the current-state sentence:

- `face_only`: use the identity image only for facial identity and facial proportions; exclude pose, expression, hair state, body, clothing, logos, accessories, lighting, and background.
- `face_clothing`: use it only for facial identity and clothing design; exclude pose, expression, hair state, body pose, lighting, and background.
- `face_body`: use it for facial identity, body proportions, tattoos, scars, and wounds; exclude pose, expression, hair state, clothing, logos, accessories, lighting, and background.
- `everything`: use the full visible appearance; exclude pose, camera composition, lighting, and background.
- `custom`: use the exact user-supplied identity-scope instruction.

The identity image receives the next available `<Picture N>` number at runtime. Do not predict, define, cite, or count that internal picture in the authored rewrite. LongCaster records both the unchanged authored prompt and the injected `effective_prompt`. It avoids inserting an instruction if the exact instruction is already present, but the rewrite should still omit it. Disabling `reinforce_state_prompt` disables these text additions; it does not by itself disable an active state guide or identity image.

## 2. Reference Labels and `subject_definitions`

Use four label types:

- `<Subject N>`: reusable visible content abstracted from reference assets, including people, animals, objects, environments, clothing, props, interfaces, visual effects, styles, actions, expressions, and poses.
- `<Picture N>`: a reference image that itself serves as a concrete keyframe, edited keyframe, composition anchor, or storyboard/shot-planning reference.
- `<Video N>`: a reference video that supplies whole-video structure, such as camera movement, cuts, rhythm, or temporal organization.
- `<Audio N>`: an audio asset or enabled audio track used for signal reuse or as a reference for timbre, delivery, dialogue, lyrics, rhythm, music, or sound texture.

Once assigned, each label must keep the same meaning across all six sections.

Count and define only the reference assets supplied to the rewrite model. LongCaster may append a historical identity image internally at generation time; that runtime image and its automatically assigned `<Picture N>` label are outside this authored label map.

Give every independently tracked referenced item its own line. State what the label denotes, its role, its important characteristics, and its source when needed. If a picture or video only supplies a subject and has no separate role later, cite it within that subject's definition rather than creating a standalone picture or video definition.

```text
<Subject 1> is the young woman in <Picture 1>, with long dark hair, a blue cardigan, and a thin silver necklace.
<Subject 2> is the warehouse environment in <Picture 2>, with corrugated walls, high steel rafters, and dim industrial practical lights.
```

One subject may draw from multiple assets, and one asset may define several subjects:

```text
<Subject 1> is the woman whose appearance comes from <Picture 1> and whose walking motion comes from <Video 1>.
```

Use standalone picture labels only when the picture itself has an additional concrete framing or planning role:

```text
<Picture 2> is a composition reference for [Shot 1], defining the low camera angle and the subject's placement.
```

Use `<Video N>` only for a whole-video structural relationship. A person, object, scene, action, or effect taken from a video remains a `<Subject N>`.

When `<Audio N>` maps to a speaking subject, reuse that speaker's global ID:

```text
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).
```

Do not create `<Audio N>` merely because a reference video happens to contain sound.

### 2.1 Stable identity versus changing state

Separate traits that define who or what a subject is from traits that may change during the timeline. Stable traits can include facial identity, facial proportions, body proportions, age, and permanent marks. Mutable state can include clothing, hairstyle condition, wetness, dirt, injuries, carried objects, accessories, pose, and expression.

If a reference picture shows clothing that will later change, define the picture's intended role narrowly instead of making that clothing part of the subject's permanent identity:

```text
<Subject 1> is the same athletic man identified from <Picture 1>, which establishes his facial identity, facial proportions, body proportions, and short dark hair. His clothing and temporary physical state may change as described in each card.
```

For a direct continuation, keep that stable definition and put the inherited current state in `summary` and at the start of `[Shot 1]`. Do not rewrite the stable definition to make every temporary state permanent, and do not describe superseded clothing from an older reference as still present.

If an earlier definition explicitly made the old clothing part of the referenced role, use `partially_preserved` when that clothing changes. If the definition limited the role to identity and body traits, `fully_preserved` remains correct because the defined role is still intact.

Choose an identity-anchor scope that agrees with the intended change. When the subject has changed out of the reference outfit, normally use `face_only` or `face_body`. An old `face_clothing` or `everything` checkpoint authorizes the old clothing and can conflict with the new state.

## 3. `summary`

Write one short paragraph beginning with a bracketed task-type prefix. For ordinary Ref2VA output, use `[reference generation]`. Add another type only when the input actually requires that additional relationship:

- `keyframe completion`: a reference image is also a concrete target frame or keyframe.
- `reference generation`: an asset guides a subject, scene, style, action, camera movement, or storyboard without being edited or continued.
- `video editing`: an existing video is directly modified.
- `video continuation`: new content continues from a source video.
- `audio reuse`: the same audio signal is reused in whole or part.
- `audio reference`: audio qualities are referenced without copying the signal.

Join multiple types with ` + ` and never repeat a type. The mere presence of video or audio does not create an editing, continuation, reuse, or audio-reference task.

For a first card or an **Independent shot**, normally use `[reference generation]`. For **Continue previous card (direct MMH3)**, use `[reference generation + video continuation]` because the accepted parent's protected joint latent is an actual continuation source. Add other types only when the requested reference relationships independently require them.

Use only labels already defined in `subject_definitions`. Summarize the target video, principal subjects, shot flow, and reference roles without introducing new labels.

## 4. `retention_analysis`

Give every defined reference label one line explaining how it is retained. Do not count newly introduced actions, settings, or story events as losses of fidelity.

Do not add a retention line for LongCaster's internal current-state guide or identity checkpoint. The runtime inserts their authority/scope instructions itself. Evaluate the authored Ref2VA labels against the intended continued state: stable identity may remain fully preserved even when a transient attribute from an older reference image has deliberately changed in the accepted parent.

For `<Subject N>`, `<Picture N>`, and `<Video N>`, use exactly one of:

- `fully_preserved`: the referenced content's defined role is fully retained.
- `partially_preserved`: it remains in use, but some defined traits change or are only partly retained.
- `attribute_transfer`: its characteristics are transferred to another identifiable subject.
- `weak_reference`: only broad style, category, composition, or atmosphere is retained.

```text
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the subject's facial identity, hairstyle, clothing, and accessories remain consistent.
<Picture 2> ([Shot 1] composition reference): fully_preserved - the low viewpoint and left-weighted composition are retained.
<Video 1> (camera movement): weak_reference - the target follows its broad arc movement without copying the source timing exactly.
```

For `<Audio N>`, use exactly one of:

- `fully_copy`: the complete source audio becomes the complete final audio track.
- `partially_copy`: only part or selected layers are copied, or copied audio is altered.
- `reference`: qualities such as timbre, rhythm, delivery, dialogue, music style, or sound texture guide the result without direct signal copying.
- `weak_reference`: only broad category or atmosphere is retained.

Do not write speaker IDs `(Sx)` in `retention_analysis`.

## 5. `detailed_description`

Write the target video in playback order. For generation tasks, normally use 350–500 English words, but prioritize accurate timing and complete dialogue over an arbitrary word count.

Establish the overall visual style and lighting in one or two sentences before `[Shot 1]`:

```text
The target video uses a cinematic live-action style with restrained contrast, natural skin detail, and dim industrial lighting.
[Shot 1] ...
```

### 5.1 Shots and cuts

`[Shot 1]` has no timestamp. Every later shot begins with a strictly increasing cut time within the video duration:

```text
[Shot 2] At 00:03.500, the camera cuts to...
```

Use sequential shot numbers. Ordinary transitions may use `the camera cuts to`, `the shot cuts to`, `the shot transitions to`, `the shot changes to`, or `the shot switches to`. Use cross-dissolves, fades, or wipes only when requested. Each cut should add new information about the subject, space, state, viewpoint, or time. Prefer camera motion when only distance or a small angle changes.

### 5.2 Applying reference labels

At the first clear appearance of an important `<Subject N>`, describe the referenced characteristics that are actually visible, its position in the frame, and its current action. Reuse the same label later without redefining it.

When a picture has a concrete framing or planning role, cite it naturally:

```text
the composition follows <Picture 1>
the shot's keyframe corresponds to <Picture 2>
the shot ends on <Picture 3>
```

Use `<Video N>` where its structural or temporal influence applies. Use `<Audio N>` in the shot or sound phase where its copy or reference relationship is active.

### 5.3 Camera motion

Write camera movement as a natural action within the shot. A complete expression may include motion type, amplitude, and speed. Omit medium amplitude and normal speed when they add no useful information.

Available motion terms include:

- `Zoom In / Zoom Out`: focal length changes while the camera stays in place.
- `Push In / Pull Out`: the camera moves forward or backward.
- `Pan Left / Pan Right`: the stationary camera pivots horizontally.
- `Truck Left / Truck Right`: the camera translates horizontally.
- `Tilt Up / Tilt Down`: the stationary camera pivots vertically.
- `Pedestal Up / Pedestal Down`: the whole camera moves vertically.
- `Arc Shot`: the camera moves in an arc around the subject.
- `Tracking Shot`: the camera follows a moving subject.
- `Static Shot`: camera position and lens remain still.
- `Shake Slightly / Shake Strongly`: slight or strong camera shake.
- `POV`: the subject's point of view.
- `Roll Clockwise / Roll Counterclockwise`: camera roll around the lens axis.
- Amplitude: `with small amplitude` or `with large amplitude`.
- Speed: `at slow speed` or `at fast speed`.

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the open doorway.
The camera holds a static shot as the runner exits the frame.
```

### 5.4 Speakers, dialogue, singing, and voiceover

Assign stable speaker IDs `(S1)`, `(S2)`, and so on in the order of actual vocal events. A speaker retains the same ID across shots. Do not assign IDs to characters who never vocalize. For multiple established speakers vocalizing together, use a compound ID such as `(S1,S2)`.

When a referenced subject speaks, retain both identifiers:

```text
<Subject 2> (S1) turns toward the woman and says, <d>[English] Last summer, I went to my grandfather's house.</d>
```

At first appearance, provide enough visible and audible information to stabilize the voice: character type, age, gender, on-screen/off-screen status, pitch, timbre, speaking rate, or accent. Keep identification, action, and delivery outside `<d>`. Inside `<d>`, include only the language tag and the user's exact spoken words. Preserve all original words and punctuation; never translate or rewrite them.

For voiceover, use the exact phrase `says in an off-screen voiceover`, followed immediately after the `<d>` block by confirmation that the corresponding on-screen character's lips remain closed:

```text
<Subject 1> (S1) says in an off-screen voiceover: <d>[English] I still remember that road.</d> while her lips remain completely closed.
```

When dialogue or lyrics cross a cut, place `<scenetrans>` at the connecting points in both portions and explicitly say the audio continues across the cut. Use `<cutoff>` when speech is truncated by the end of the video.

When dialogue, narration, or lyrics from reference audio are directly reused or explicitly reperformed, preserve the exact source words and language. Use `[unclear]` for unintelligible spans. When only timbre, rhythm, emotion, or delivery is referenced, do not import the source's verbal content.

If words are only part of a directly reused soundtrack and no independent character or narrator produces them, use `<Audio N>` as the source and do not invent `(Sx)`.

### 5.5 Visible text

Put every banner, sign, label, subtitle, screen display, or neon inscription that is actually visible in English double quotation marks. Preserve its original text and punctuation without translation:

```text
A red neon sign reading "营业中" glows above the doorway.
```

### 5.6 Writing a LongCaster clip continuation

For **Continue previous card (direct MMH3)**, write the six sections for the newly visible portion of the next card. The hidden 39-frame handover is supplied automatically and must not be described as an extra opening shot or added to the requested duration. Treat shot timestamps as positions in the newly visible card; do not add a 1.625-second offset.

The rewrite model should use these inputs when available:

- the continuation strategy: direct MMH3 or independent;
- the requested newly visible duration;
- the accepted parent's final visible state, including pose, motion direction and speed, camera position and movement, environment, lighting, clothing, hair condition, wetness, props, damage, and active sound;
- the intended next action and any intentional state change;
- the persistent Ref2VA label map and retention requirements;
- the active identity scope, without inventing its runtime picture number.

If the parent's final state is not supplied, do not invent precise carry-over details. State the continuation relationship conservatively, retain established subject traits, and describe only the new action requested by the user.

For a direct continuation:

1. Keep stable `subject_definitions` consistent with the prior card. Do not redefine identity merely because pose, expression, clothing condition, or another temporary state changed.
2. Begin `summary` with `[reference generation + video continuation]`. Summarize the new visible segment and its intended change from the prior ending.
3. In `retention_analysis`, preserve stable reference roles without commanding a return to an older appearance. If the accepted parent intentionally removed a hat, changed clothes, became wet, picked up an object, or sustained visible damage, treat that current state as the starting authority.
4. Begin `[Shot 1]` as a continuous boundary, using verbs such as `continues`, `remains`, `completes`, `slows`, or `resumes`. State the inherited pose/action, screen position, movement vector, camera motion, lighting, and active sound that are visible at the start.
5. Describe only new visible development after the boundary. Do not narrate the hidden handover frames, replay the previous ending, or introduce the subject and location as if they have appeared for the first time.
6. Preserve camera inertia unless the user requests a cut or a deliberate movement change. For a cut, state the cut after the continuity boundary and give its time relative to the new visible segment.
7. Continue ambience, action sounds, dialogue, and music cleanly across the boundary when they persist. State intentional stops, fades, impacts, or changes explicitly.

For example, if the accepted parent shows the same man after he has changed from street clothes into swimming trunks, write the next card like this:

```text
subject_definitions:
<Subject 1> is the same athletic man identified from <Picture 1>, which establishes his facial identity, facial proportions, body proportions, and short dark hair. Clothing and temporary condition are timeline state rather than fixed identity traits.

summary:
[reference generation + video continuation] Continuing from the previous accepted card, <Subject 1> remains in the swimming-pool setting wearing the black swimming trunks established before the boundary and proceeds toward the pool.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - his facial identity, body proportions, and permanent traits remain consistent; the earlier reference outfit is intentionally superseded by his current black swimming trunks.

detailed_description:
The target video continues the established realistic visual style and pool lighting.
[Shot 1] The clip continues seamlessly from the previous accepted card. <Subject 1> remains in the same black swimming trunks shown at the accepted ending, with the same current hair condition, skin wetness, pose, and position carried across the boundary. He continues walking toward the pool as the camera preserves its existing movement.

overall_soundscape:
The established indoor pool ambience, footsteps, and water movement continue across the boundary without a restart.

non_diegetic_music:
N/A
```

Do not say that he changes into the trunks again, and do not list the old outfit as part of his current appearance. LongCaster's current-state anchor supplies the accepted visual state; the authored prompt names the visible state so the intended continuation is unambiguous.

Avoid reset-prone wording such as `the video begins with`, `the subject appears`, `the scene opens on a new view`, or a full restatement of the original reference appearance when the same shot is continuing. Prefer a concrete boundary sentence:

```text
[Shot 1] The clip continues seamlessly from the previous accepted card. <Subject 1> remains at the right side of the pool in the same soaked black trunks, finishing his turn as the camera continues its slow rightward track. Water movement and indoor pool ambience carry across the boundary without a break. He then plants both feet, removes his goggles, and looks toward the camera.
```

For an **Independent shot**, do not imply latent or state continuity. Write a complete opening composition and use `[reference generation]`, even if the card happens to reuse subjects or a setting from an earlier card.

## 6. `overall_soundscape`

Use one continuous paragraph of 1–4 English sentences summarizing ambient sound, physical action sounds, and non-verbal human sounds across the whole video. Examples include wind, rain, traffic, footsteps, fabric, impacts, breathing, laughter, and panting.

Dialogue, singing, and diegetic music belong in `detailed_description` and must not be repeated here. Use `N/A` only if the user explicitly requests complete silence.

When reference audio supplies ambience or sound effects, state its copy or reference relationship here.

## 7. `non_diegetic_music`

Use 1–3 English sentences for background music audible only to the audience. Describe instrumentation, tempo, rhythm, and dynamic development. Do not rely on abstract mood labels or explain the score's emotional purpose.

Music from an instrument, radio, television, phone, or other source audible to characters is diegetic and belongs in `detailed_description`. Use `N/A` when there is no non-diegetic music. When reference audio supplies the audience-only score, state its copy or reference relationship here.

## 8. Ref2VA Output Template

```text
subject_definitions:
<Subject 1> is ... in <Picture 1>, characterized by ...
<Subject 2> is ... in <Picture 2>, characterized by ...

summary:
[reference generation] The target video ...

retention_analysis:
<Subject 1> (appears in [Shot 1], ...): fully_preserved - ...
<Subject 2> (appears in [Shot 1], ...): fully_preserved - ...

detailed_description:
The target video uses ...
[Shot 1] ...
[Shot 2] At 00:SS.mmm, the camera cuts to ...

overall_soundscape:
...

non_diegetic_music:
N/A
```

### 8.1 Direct-continuation template

Do not include any automatic anchor instructions in this authored output.

```text
subject_definitions:
<Subject 1> is ... [retain the established stable definition and reference source]

summary:
[reference generation + video continuation] Continuing seamlessly from the previous accepted card, the new visible segment ...

retention_analysis:
<Subject 1> (appears in [Shot 1], ...): fully_preserved - the stable identity and intended reference traits remain consistent while the accepted parent's current visible state carries forward.

detailed_description:
The target video continues the established visual style and lighting.
[Shot 1] The clip continues seamlessly from the previous accepted card. <Subject 1> remains ... as the camera continues ... and the existing ambience carries across the boundary. The new action then ...
[Shot 2] At 00:SS.mmm, the camera cuts to ...

overall_soundscape:
The established ambience continues across the boundary ...

non_diegetic_music:
The existing score continues without a restart ...
```

## 9. Final Validation

Before returning the rewrite, verify that:

- All six sections appear once and in the required order.
- Every reference label is defined before use and retains one meaning.
- Images used only as sources for subjects are not redundantly defined as standalone pictures.
- Every defined reference label has a `retention_analysis` entry.
- No authored label or retention line was invented for LongCaster's internal state or identity anchors.
- Ref2VA subjects are described at their first visible appearance and remain consistent.
- `[Shot 1]` has no timestamp; later cuts are sequential and within duration.
- Camera directions are natural actions, not stacked tags.
- Speaker IDs are stable, and dialogue/lyrics remain verbatim inside `<d>`.
- Voiceover explicitly keeps the matching on-screen lips closed.
- Visible text remains verbatim inside English double quotation marks.
- Dialogue and diegetic music are not duplicated in `overall_soundscape` or `non_diegetic_music`.
- A direct continuation uses `[reference generation + video continuation]`, starts from the accepted parent's final visible state, and describes only the newly visible duration.
- A direct continuation does not add 1.625 seconds to timestamps, narrate the hidden 39-frame handover, duplicate LongCaster's automatic instructions, or restore superseded appearance details from an older reference.
- An independent card uses a complete opening rather than implying latent continuation.
- No instructions specific to T2VA, I2VA, FL2VA, or L2VA have been added.
