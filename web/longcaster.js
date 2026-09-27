import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createCardReferenceControls, referenceSettings } from "./card_reference.js";
import { assemblePictureReferences, createPictureReferenceRail } from "./picture_reference_rail.js";

function widget(node, name) {
    return node.widgets?.find((item) => item.name === name);
}

function repairContinuationTreatmentWidgetOrder(node) {
    const treatment = widget(node, "continuation_treatment");
    const cadence = widget(node, "refine_cadence");
    const sampler = widget(node, "refine_sampler_name");
    const seedMode = widget(node, "refine_seed_mode");
    if (!treatment || !cadence || !sampler || !seedMode) return false;
    const cadences = new Set(["off", "every_accepted_card", "manual"]);
    const seedModes = new Set(["inherit", "offset"]);
    const treatments = new Set(["off", "drift_control", "color_stable"]);
    // A workflow saved while the bad order was active is read by the fixed
    // order as cadence=<treatment>, sampler=<cadence>, seed=<sampler>, and
    // treatment=<seed>. Restore it before generic invalid-value cleanup.
    if (cadences.has(String(sampler.value))) {
        const oldCadence = String(sampler.value);
        const oldSampler = seedModes.has(String(seedMode.value))
            ? "euler" : String(seedMode.value);
        const oldSeedMode = seedModes.has(String(treatment.value))
            ? String(treatment.value)
            : seedModes.has(String(seedMode.value)) ? String(seedMode.value) : "inherit";
        const oldTreatment = treatments.has(String(treatment.value))
            ? String(treatment.value)
            : treatments.has(String(cadence.value)) ? String(cadence.value) : "off";
        cadence.value = oldCadence;
        sampler.value = oldSampler;
        seedMode.value = oldSeedMode;
        treatment.value = oldTreatment;
        return true;
    }
    // One release inserted continuation_treatment before the established
    // refine widgets. Repair workflows saved with that positional shift.
    if (seedModes.has(String(sampler.value)) && !cadences.has(String(cadence.value))) {
        const oldCadence = String(treatment.value);
        const oldSampler = String(cadence.value);
        const oldSeedMode = String(sampler.value);
        cadence.value = cadences.has(oldCadence) ? oldCadence : "off";
        sampler.value = oldSampler;
        seedMode.value = oldSeedMode;
        treatment.value = "off";
        return true;
    }
    // With the corrected append-only order, a workflow saved during the bad
    // release restores the three refine values but leaves the new tail invalid.
    if (!treatments.has(String(treatment.value))) {
        treatment.value = "off";
        return true;
    }
    return false;
}

function commandId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isNodeType(node, name) {
    return node?.comfyClass === name || node?.type === name;
}

function projectAuthority(node) {
    const graph = node?.graph;
    if (isNodeType(node, "LongCasterProject")) return node;
    if (graph?.longcasterProjectAuthority) return graph.longcasterProjectAuthority;
    return graph?._nodes?.find((item) => isNodeType(item, "LongCasterProject") && item.longcasterCardsWorkspace);
}

function syncProjectNode(node, state) {
    const card = state.active_card || state.cards?.find((item) => item.id === state.active_card_id);
    const values = {
        project_name: state.project, generation_mode: state.generation_mode,
        width: state.width, height: state.height,
        prompt: card?.assembled_prompt ?? card?.prompt,
        duration_seconds: card?.requested_duration_seconds, seed: card?.seed,
        ref_image_size: card?.ref_image_size,
        refine_cadence: state.refine_cadence,
        continuation_treatment: state.continuation_treatment,
    };
    for (const [name, value] of Object.entries(values)) {
        const item = widget(node, name);
        if (item && value !== undefined) item.value = value;
    }
    node.longcasterState = { ...state, active_card: card, card_count: state.card_count ?? state.cards?.length };
    node.title = `MiniMax H3 LongCaster · ${state.project} · ${card?.status ?? "UNKNOWN"}`;
    for (const dependent of node.graph?._nodes || []) {
        if (!isNodeType(dependent, "LongCasterIdentityAnchor")
            && !isNodeType(dependent, "LongCasterTimelineExport")
            && !isNodeType(dependent, "LongCasterCardLoras")) continue;
        const project = widget(dependent, "project_name");
        if (project) project.value = state.project;
        dependent.longcasterProjectState = state;
        if (isNodeType(dependent, "LongCasterCardLoras")) {
            dependent.title = `Card LoRAs · ${state.project} · Card ${(card?.timeline_index ?? 0) + 1}`;
        } else if (isNodeType(dependent, "LongCasterTimelineExport")) {
            dependent.title = `Join ${state.project} cards (enable, then Queue)`;
        } else if (dependent.longcasterIdentityProjectSelect) {
            refreshIdentityProject(dependent, state.project);
        }
    }
    node.graph?.setDirtyCanvas(true, true);
}

function remainingBatchPlan(state) {
    const cards = state?.cards || [];
    const start = cards.findIndex((card) => card.id === state?.active_card_id);
    if (start < 0) return null;
    const tail = cards.slice(start);
    const first = tail[0];
    const generatable = new Set(["EMPTY", "FAILED", "INVALIDATED"]);
    if (![...generatable, "DRAFT"].includes(first.status)) return null;
    if (tail.slice(1).some((card) => card.status !== "INVALIDATED")) return null;
    if (first.status === "DRAFT" && (first.draft_inputs_dirty || tail.length === 1)) return null;
    const renderCount = tail.filter((card) => generatable.has(card.status)).length;
    if (!renderCount) return null;
    return {
        firstCardId: first.id,
        lastCardId: tail[tail.length - 1].id,
        firstAction: first.status === "DRAFT" ? "accept" : "generate",
        renderCount,
        cardCount: tail.length,
    };
}

async function queueAction(node, action) {
    const authority = projectAuthority(node);
    if (authority) repairContinuationTreatmentWidgetOrder(authority);
    const workspace = authority?.longcasterCardsWorkspace;
    if (workspace && action !== "cancel") {
        if (workspace.loading && !await workspace.loading) return null;
        if (!await workspace.flush()) return null;
        if (workspace.state) syncProjectNode(authority, workspace.state);
    }
    if (authority && authority !== node) {
        const projectAction = widget(authority, "action");
        const projectCommand = widget(authority, "command_id");
        if (projectAction) projectAction.value = "resume";
        if (projectCommand) projectCommand.value = commandId();
    }
    const actionWidget = widget(node, "action");
    const commandWidget = widget(node, "command_id");
    if (actionWidget) actionWidget.value = action;
    if (commandWidget) commandWidget.value = commandId();
    node.graph?.setDirtyCanvas(true, true);
    workspace?.execution?.begin(action);
    try {
        const queued = await app.queuePrompt(0, 1);
        workspace?.execution?.bindPrompt(queued?.prompt_id);
        return queued;
    } catch (error) {
        workspace?.execution?.fail(`Queue failed: ${error.message}`);
        return null;
    }
}

async function stopAndUnlock(node) {
    const workspace = projectAuthority(node)?.longcasterCardsWorkspace;
    workspace?.execution?.stopping();
    try {
        const response = await api.fetchApi("/interrupt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        workspace?.execution?.append("warning", "Interrupt acknowledged by ComfyUI. Queueing project unlock.");
    } catch (error) {
        workspace?.execution?.append("error", `Interrupt request failed: ${error.message}`);
    } finally {
        await queueAction(node, "cancel");
    }
}

function hideWidget(item) {
    if (!item) return;
    item.computeSize = () => [0, -4];
    item.type = "hidden";
}

async function responseJson(response) {
    const body = await response.json();
    if (!response.ok) throw new Error(body?.error || `${response.status} ${response.statusText}`);
    return body;
}

function selectedSource(node) {
    const value = String(widget(node, "source_card")?.value ?? "");
    return node.longcasterIdentityState?.accepted_sources?.find(
        (item) => String(item.card) === value || item.card_id === value,
    );
}

function setIdentityFrame(node, frame, seekVideo = true) {
    const source = selectedSource(node);
    const maximum = source?.max_frame_index ?? 0;
    const value = Math.max(0, Math.min(maximum, Math.round(Number(frame) || 0)));
    const frameWidget = widget(node, "frame_index");
    if (frameWidget) frameWidget.value = value;
    if (node.longcasterIdentityTime) {
        node.longcasterIdentityTime.textContent = `${(value / 24).toFixed(3)}s · frame ${value}`;
    }
    const video = node.longcasterIdentityVideo;
    if (seekVideo && video?.src && Math.abs(video.currentTime - value / 24) > 0.02) {
        video.currentTime = value / 24;
    }
    node.graph?.setDirtyCanvas(true, true);
}

function updateIdentityPanel(node, state) {
    node.longcasterIdentityState = state;
    const sourceWidget = widget(node, "source_card");
    const active = state?.active_identity_anchor;
    const scopeWidget = widget(node, "identity_scope");
    const customWidget = widget(node, "custom_identity_instruction");
    if (active?.identity_scope && scopeWidget) scopeWidget.value = active.identity_scope;
    if (active && customWidget) customWidget.value = active.custom_identity_instruction || "";
    const projectSelect = node.longcasterIdentityProjectSelect;
    const cardSelect = node.longcasterIdentityCardSelect;
    if (projectSelect && state?.project) projectSelect.value = state.project;
    if (cardSelect) {
        const previous = String(sourceWidget?.value ?? "");
        cardSelect.replaceChildren();
        for (const source of state?.accepted_sources || []) {
            const option = document.createElement("option");
            option.value = String(source.card);
            option.textContent = `Card ${source.card} · ${((source.preview_frames || 1) / 24).toFixed(2)}s${source.preview_available ? "" : " · no saved preview"}`;
            cardSelect.append(option);
        }
        const match = (state?.accepted_sources || []).find(
            (item) => String(item.card) === previous || item.card_id === previous,
        );
        const activeChoice = (state?.accepted_sources || []).find(
            (item) => item.card_id === state?.active_source?.card_id,
        );
        const chosen = match || activeChoice || state?.accepted_sources?.[0];
        if (chosen) {
            cardSelect.value = String(chosen.card);
            if (sourceWidget) sourceWidget.value = String(chosen.card);
            if (!match && chosen.card_id === state?.active_source?.card_id) {
                const frameWidget = widget(node, "frame_index");
                if (frameWidget) frameWidget.value = state.active_source.preview_frame_index ?? 0;
            }
        }
    }
    node.title = active
        ? `LongCaster Identity · ${active.enabled ? "ACTIVE" : "DISABLED"}`
        : "LongCaster Identity · NONE";
    if (node.longcasterIdentityEnabled) node.longcasterIdentityEnabled.checked = Boolean(active?.enabled);
    if (node.longcasterIdentityStatus) node.longcasterIdentityStatus.textContent = state?.message || "Ready";
    const source = selectedSource(node);
    const video = node.longcasterIdentityVideo;
    if (node.longcasterIdentityBuildPreview) {
        node.longcasterIdentityBuildPreview.disabled = Boolean(source?.preview_available);
        node.longcasterIdentityBuildPreview.textContent = source?.preview_available ? "Preview ready" : "Build preview";
    }
    if (video) {
        if (source?.preview_available) {
            const project = encodeURIComponent(state.project);
            const card = encodeURIComponent(source.card_id);
            const nextSource = api.apiURL(`/longcaster/identity/preview?project=${project}&card=${card}`);
            if (video.dataset.source !== nextSource) {
                video.dataset.source = nextSource;
                video.src = nextSource;
                video.load();
            }
        } else {
            video.removeAttribute("src");
            video.dataset.source = "";
            video.load();
        }
    }
    setIdentityFrame(node, widget(node, "frame_index")?.value ?? 0);
    node.graph?.setDirtyCanvas(true, true);
}

async function refreshIdentityProjects(node) {
    const authority = projectAuthority(node);
    const project = authority?.longcasterCardsWorkspace?.state?.project
        || widget(authority, "project_name")?.value
        || widget(node, "project_name")?.value;
    return refreshIdentityProject(node, project);
}

async function refreshIdentityProject(node, project) {
    if (!project) return false;
    const epoch = (node.longcasterIdentityLoadEpoch || 0) + 1;
    node.longcasterIdentityLoadEpoch = epoch;
    const projectWidget = widget(node, "project_name");
    if (projectWidget) projectWidget.value = project;
    const select = node.longcasterIdentityProjectSelect;
    if (select) {
        select.replaceChildren();
        const option = document.createElement("option");
        option.value = project;
        option.textContent = project;
        select.append(option);
        select.value = project;
        select.disabled = true;
    }
    try {
        const subject = encodeURIComponent(widget(node, "subject_id")?.value || "<Subject 1>");
        const state = await responseJson(await api.fetchApi(
            `/longcaster/identity/state?project=${encodeURIComponent(project)}&subject_id=${subject}`,
        ));
        if (epoch !== node.longcasterIdentityLoadEpoch || widget(node, "project_name")?.value !== project) return false;
        updateIdentityPanel(node, state);
        return true;
    } catch (error) {
        if (epoch === node.longcasterIdentityLoadEpoch) node.longcasterIdentityStatus.textContent = `Refresh failed: ${error.message}`;
        return false;
    }
}

async function identityControl(node, action) {
    try {
        const response = await api.fetchApi("/longcaster/identity/control", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                project: widget(node, "project_name")?.value,
                subject_id: widget(node, "subject_id")?.value,
                action,
            }),
        });
        updateIdentityPanel(node, await responseJson(response));
    } catch (error) {
        node.longcasterIdentityStatus.textContent = `${action} failed: ${error.message}`;
    }
}

function createIdentityPicker(node) {
    const root = document.createElement("div");
    Object.assign(root.style, { display: "grid", gap: "6px", padding: "6px", color: "#ddd", fontSize: "12px" });

    const projectRow = document.createElement("div");
    Object.assign(projectRow.style, { display: "grid", gridTemplateColumns: "70px 1fr auto", gap: "6px", alignItems: "center" });
    const projectLabel = document.createElement("span"); projectLabel.textContent = "Project";
    const projectSelect = document.createElement("select");
    projectSelect.disabled = true;
    const refresh = document.createElement("button"); refresh.textContent = "Refresh";
    projectRow.append(projectLabel, projectSelect, refresh);

    const cardRow = document.createElement("div");
    Object.assign(cardRow.style, { display: "grid", gridTemplateColumns: "70px 1fr auto", gap: "6px", alignItems: "center" });
    const cardLabel = document.createElement("span"); cardLabel.textContent = "Face source";
    const cardSelect = document.createElement("select");
    const buildPreview = document.createElement("button"); buildPreview.textContent = "Build preview";
    cardRow.append(cardLabel, cardSelect, buildPreview);

    const video = document.createElement("video");
    video.controls = true;
    video.preload = "metadata";
    Object.assign(video.style, { width: "100%", maxHeight: "250px", background: "#080808", borderRadius: "5px" });

    const time = document.createElement("div");
    time.textContent = "0.000s · frame 0";
    Object.assign(time.style, { textAlign: "center", fontVariantNumeric: "tabular-nums" });

    const frameRow = document.createElement("div");
    Object.assign(frameRow.style, { display: "grid", gridTemplateColumns: "auto auto 1fr", gap: "6px" });
    const previous = document.createElement("button"); previous.textContent = "◀ 1 frame";
    const next = document.createElement("button"); next.textContent = "1 frame ▶";
    const exact = document.createElement("button"); exact.textContent = "Preview exact MMH3 frame";
    frameRow.append(previous, next, exact);

    const actionRow = document.createElement("div");
    Object.assign(actionRow.style, { display: "grid", gridTemplateColumns: "1fr auto auto", gap: "6px" });
    const use = document.createElement("button"); use.textContent = "Use Current Frame as Identity";
    const enabledLabel = document.createElement("label");
    const enabled = document.createElement("input"); enabled.type = "checkbox";
    enabledLabel.append(enabled, document.createTextNode(" Enabled"));
    const clear = document.createElement("button"); clear.textContent = "Clear";
    actionRow.append(use, enabledLabel, clear);

    const status = document.createElement("div");
    status.textContent = "Refresh to load projects and accepted cards.";
    Object.assign(status.style, { color: "#aaa", minHeight: "16px" });
    root.append(projectRow, cardRow, video, time, frameRow, actionRow, status);

    node.longcasterIdentityProjectSelect = projectSelect;
    node.longcasterIdentityCardSelect = cardSelect;
    node.longcasterIdentityBuildPreview = buildPreview;
    node.longcasterIdentityVideo = video;
    node.longcasterIdentityTime = time;
    node.longcasterIdentityEnabled = enabled;
    node.longcasterIdentityStatus = status;

    refresh.onclick = () => refreshIdentityProjects(node);
    cardSelect.onchange = () => {
        const sourceWidget = widget(node, "source_card");
        if (sourceWidget) sourceWidget.value = cardSelect.value;
        setIdentityFrame(node, 0);
        updateIdentityPanel(node, node.longcasterIdentityState);
    };
    buildPreview.onclick = () => queueAction(node, "build_preview");
    const syncPlayhead = () => setIdentityFrame(node, Math.round(video.currentTime * 24), false);
    video.addEventListener("pause", syncPlayhead);
    video.addEventListener("seeked", syncPlayhead);
    previous.onclick = () => setIdentityFrame(node, (widget(node, "frame_index")?.value || 0) - 1);
    next.onclick = () => setIdentityFrame(node, (widget(node, "frame_index")?.value || 0) + 1);
    exact.onclick = () => queueAction(node, "preview");
    use.onclick = () => queueAction(node, "set");
    enabled.onchange = () => identityControl(node, enabled.checked ? "enable" : "disable");
    clear.onclick = () => identityControl(node, "clear");

    node.addDOMWidget("identity_picker", "identity-picker", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 360,
    });
}

function isLongCasterVhs(node) {
    return String(widget(node, "format")?.value || "").includes("longcaster_nvenc_h264-mp4");
}

function disableOriginalVhsPlayback(node) {
    const preview = widget(node, "videopreview");
    if (!preview || !isLongCasterVhs(node)) return;
    if (typeof preview.value !== "object" || preview.value === null) preview.value = {};
    preview.value.paused = true;
    const video = preview.videoEl;
    if (video) {
        video.autoplay = false;
        video.loop = false;
        video.pause();
    }
}

function ensureLongCasterVhsPlayer(node) {
    if (!isLongCasterVhs(node)) return;
    disableOriginalVhsPlayback(node);
    const originalPreview = widget(node, "videopreview");
    if (originalPreview?.parentEl) originalPreview.parentEl.hidden = true;
    if (node.longcasterVhsPlayer) return;

    const root = document.createElement("div");
    Object.assign(root.style, { width: "100%", display: "grid", gap: "4px", padding: "4px" });
    const video = document.createElement("video");
    video.controls = true;
    video.autoplay = false;
    video.loop = false;
    video.preload = "metadata";
    Object.assign(video.style, { width: "100%", maxHeight: "360px", background: "#080808", borderRadius: "5px" });
    const status = document.createElement("div");
    status.textContent = "Preview loads paused.";
    Object.assign(status.style, { color: "#aaa", fontSize: "11px", textAlign: "center" });
    root.append(video, status);
    node.longcasterVhsPlayer = video;
    node.longcasterVhsStatus = status;
    node.addDOMWidget("longcaster_video_player", "longcaster-video-player", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 260,
    });
    const computed = node.computeSize?.();
    node.setSize([Math.max(node.size[0], 380), Math.max(node.size[1], computed?.[1] ?? node.size[1])]);
}

function loadLongCasterVhsResult(node, message) {
    if (!isLongCasterVhs(node)) return;
    ensureLongCasterVhsPlayer(node);
    disableOriginalVhsPlayback(node);
    const preview = message?.gifs?.[0];
    if (!preview || !node.longcasterVhsPlayer) return;
    const params = {
        filename: preview.filename,
        subfolder: preview.subfolder || "",
        type: preview.type || "output",
    };
    const video = node.longcasterVhsPlayer;
    video.pause();
    video.autoplay = false;
    video.src = api.apiURL(`/view?${new URLSearchParams(params)}`);
    video.load();
    if (node.longcasterVhsStatus) node.longcasterVhsStatus.textContent = "Paused · press Play to review";
    setTimeout(() => disableOriginalVhsPlayback(node), 150);
}

const CARD_SECTIONS = [
    ["subject_definitions", "Subject Definitions"],
    ["summary", "Summary"],
    ["retention_analysis", "Retention Analysis"],
    ["detailed_description", "Detailed Description"],
    ["overall_soundscape", "Overall Soundscape"],
    ["non_diegetic_music", "Non-Diegetic Music"],
];

const PROJECT_ASPECT_RATIOS = [
    ["1:1 (Square)", 1, 1],
    ["2:3 (Portrait Photo)", 2, 3],
    ["3:2 (Photo)", 3, 2],
    ["3:4 (Portrait Standard)", 3, 4],
    ["4:3 (Standard)", 4, 3],
    ["9:16 (Portrait Widescreen)", 9, 16],
    ["16:9 (Widescreen)", 16, 9],
    ["21:9 (Ultrawide)", 21, 9],
];
const PROJECT_RESOLUTION_MULTIPLE = 32;

function projectResolution(aspectLabel, megapixels) {
    const aspect = PROJECT_ASPECT_RATIOS.find(([label]) => label === aspectLabel) || PROJECT_ASPECT_RATIOS[0];
    const target = Math.min(16, Math.max(0.1, Number(megapixels) || 0.1)) * 1024 * 1024;
    const scale = Math.sqrt(target / (aspect[1] * aspect[2]));
    return {
        width: Math.max(PROJECT_RESOLUTION_MULTIPLE, Math.round(aspect[1] * scale / PROJECT_RESOLUTION_MULTIPLE) * PROJECT_RESOLUTION_MULTIPLE),
        height: Math.max(PROJECT_RESOLUTION_MULTIPLE, Math.round(aspect[2] * scale / PROJECT_RESOLUTION_MULTIPLE) * PROJECT_RESOLUTION_MULTIPLE),
    };
}

function nearestProjectAspect(width, height) {
    const ratio = Math.max(1, Number(width)) / Math.max(1, Number(height));
    return PROJECT_ASPECT_RATIOS.reduce((best, candidate) => (
        Math.abs(Math.log(candidate[1] / candidate[2]) - Math.log(ratio))
            < Math.abs(Math.log(best[1] / best[2]) - Math.log(ratio)) ? candidate : best
    ))[0];
}

function cardsElement(tag, className, textContent) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (textContent !== undefined) element.textContent = textContent;
    return element;
}

async function copyTextToClipboard(text) {
    const value = String(text ?? "");
    if (globalThis.navigator?.clipboard?.writeText) {
        try {
            await globalThis.navigator.clipboard.writeText(value);
            return;
        } catch (_) {
            // Fall through for browsers that expose Clipboard but deny access.
        }
    }
    const fallback = document.createElement("textarea");
    fallback.value = value;
    fallback.readOnly = true;
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.append(fallback);
    fallback.select();
    const copied = document.execCommand?.("copy");
    fallback.remove();
    if (!copied) throw new Error("clipboard access is unavailable");
}

function promptLineDiff(archivedPrompt, workingPrompt) {
    const archived = String(archivedPrompt ?? "").split("\n");
    const working = String(workingPrompt ?? "").split("\n");
    const columns = working.length + 1;
    const cellCount = (archived.length + 1) * columns;
    if (cellCount > 4_000_000) {
        return [
            ...archived.map((line, index) => ({kind: "delete", line, archivedLine: index + 1, workingLine: null})),
            ...working.map((line, index) => ({kind: "add", line, archivedLine: null, workingLine: index + 1})),
        ];
    }
    const lengths = new Uint32Array(cellCount);
    for (let left = archived.length - 1; left >= 0; left -= 1) {
        for (let right = working.length - 1; right >= 0; right -= 1) {
            const cell = left * columns + right;
            lengths[cell] = archived[left] === working[right]
                ? lengths[(left + 1) * columns + right + 1] + 1
                : Math.max(lengths[(left + 1) * columns + right], lengths[left * columns + right + 1]);
        }
    }
    const result = [];
    let left = 0;
    let right = 0;
    while (left < archived.length || right < working.length) {
        if (left < archived.length && right < working.length && archived[left] === working[right]) {
            result.push({kind: "equal", line: archived[left], archivedLine: left + 1, workingLine: right + 1});
            left += 1;
            right += 1;
        } else if (
            left < archived.length
            && (right >= working.length
                || lengths[(left + 1) * columns + right] >= lengths[left * columns + right + 1])
        ) {
            result.push({kind: "delete", line: archived[left], archivedLine: left + 1, workingLine: null});
            left += 1;
        } else {
            result.push({kind: "add", line: working[right], archivedLine: null, workingLine: right + 1});
            right += 1;
        }
    }
    return result;
}

function differentRandomSeed(currentSeed) {
    const maximum = 0xFFFFFFFF;
    let candidate;
    if (globalThis.crypto?.getRandomValues) {
        const values = new Uint32Array(1);
        globalThis.crypto.getRandomValues(values);
        candidate = Number(values[0]);
    } else {
        candidate = Math.floor(Math.random() * (maximum + 1));
    }
    const current = Number(currentSeed);
    if (candidate === current) candidate = (candidate + 1) % (maximum + 1);
    return candidate;
}

function differentUnusedSeed(currentSeed, usedSeeds) {
    let candidate = differentRandomSeed(currentSeed);
    while (usedSeeds.has(candidate)) candidate = (candidate + 1) >>> 0;
    usedSeeds.add(candidate);
    return candidate;
}

function createCardsWorkspace(node) {
    if (node.longcasterCardsWorkspace) {
        node.longcasterCardsWorkspace.root.hidden = false;
        node.longcasterCardsWorkspace.load();
        return;
    }

    const root = cardsElement("div", "lc-cards-overlay");
    const style = document.createElement("style");
    style.textContent = `
        .lc-cards-overlay{position:fixed;inset:0;z-index:10020;background:#08131d;color:#dce8f3;font:13px Inter,system-ui,sans-serif;display:grid;grid-template-rows:auto auto minmax(0,1fr) auto auto}
        .lc-cards-overlay[hidden],.lc-cards-overlay [hidden]{display:none!important}.lc-cards-top{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#0c1c29;border-bottom:1px solid #254052}
        .lc-cards-brand{font-size:20px;font-weight:700;margin-right:8px}.lc-cards-top select,.lc-cards-overlay input,.lc-cards-overlay select,.lc-cards-overlay textarea{background:#0d2131;color:#e7f1f8;border:1px solid #29465b;border-radius:5px;padding:7px;box-sizing:border-box}
        .lc-cards-top button,.lc-cards-overlay button{background:#173149;color:#dce8f3;border:1px solid #31516a;border-radius:5px;padding:7px 11px;cursor:pointer}.lc-cards-overlay button:hover:not(:disabled){background:#1f4565}.lc-cards-overlay button:disabled{opacity:.42;cursor:default}
        .lc-cards-primary{background:#0968bd!important;border-color:#1684e6!important}.lc-cards-accept{background:#088653!important;border-color:#0ebd72!important}.lc-cards-danger{background:#8f2633!important;border-color:#d95764!important}.lc-cards-close{margin-left:auto}
        .lc-cards-projectpath{color:#89a2b4;font:11px ui-monospace,monospace}.lc-cards-new{display:grid;grid-template-columns:2fr repeat(6,minmax(90px,1fr)) auto auto;gap:8px;align-items:end;padding:10px 14px;background:#102333;border-bottom:1px solid #29465b}.lc-cards-new[hidden]{display:none}.lc-cards-new label{display:grid;gap:4px;color:#a9bdca}.lc-cards-resolution{min-height:31px;display:flex;align-items:center;color:#dce8f3;font:12px ui-monospace,monospace;white-space:nowrap}
        .lc-studio-tabs{display:flex;gap:5px;padding:7px 14px;background:#0a1925;border-bottom:1px solid #254052}.lc-studio-tab{min-width:110px}.lc-studio-tab.active{background:#0968bd!important;border-color:#1684e6!important}.lc-cards-project{min-height:0;overflow:auto;padding:14px;display:grid;grid-template-columns:minmax(520px,1fr) minmax(340px,520px);gap:14px;align-items:start}.lc-project-panel{min-width:0;background:#0a1824;border:1px solid #20394b;border-radius:7px;padding:12px}.lc-project-panel h2{margin:0 0 5px}.lc-project-identity-source{display:grid;grid-template-columns:minmax(180px,1fr) auto;gap:8px;align-items:end;margin-top:12px}.lc-project-identity-source label{display:grid;gap:4px;color:#a9bdca}.lc-project-identity-video{grid-column:1/-1;width:100%;max-height:340px;background:#02070a;border-radius:6px}.lc-project-identity-form{grid-column:1/-1;display:grid;grid-template-columns:minmax(140px,1fr) 110px minmax(180px,1fr) minmax(150px,1fr);gap:7px}.lc-project-identity-form label{display:grid;gap:4px}.lc-project-identity-form .wide{grid-column:1/-1}.lc-project-identity-form textarea{min-height:70px;resize:vertical}
        .lc-cards-main{min-height:0;display:grid;grid-template-columns:260px minmax(620px,1fr) 420px}.lc-cards-sidebar,.lc-cards-preview{min-height:0;overflow:auto;background:#0a1824;padding:10px;border-right:1px solid #20394b}.lc-cards-preview{border-right:0;border-left:1px solid #20394b}
        .lc-cards-list{display:grid;gap:5px}.lc-card-row{display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:5px}.lc-card-item{min-width:0;display:grid!important;grid-template-columns:34px minmax(0,1fr);gap:7px;text-align:left;padding:9px!important}.lc-card-copy{align-self:stretch;width:34px;padding:4px!important;font-size:18px;line-height:1}.lc-card-item.selected{border-color:#168cf0;background:#123b5c}.lc-card-number{font-weight:700}.lc-card-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lc-card-meta{font-size:11px;color:#93a9b9;margin-top:3px}.lc-status-ACCEPTED,.lc-render-VALIDATED{color:#35d181}.lc-status-DRAFT{color:#53aaff}.lc-status-FAILED{color:#ff6666}.lc-status-INVALIDATED,.lc-render-INVALIDATED{color:#f5bd4d}.lc-status-EMPTY{color:#94a4b0}
        .lc-cards-editor{min-width:0;overflow:auto;padding:12px;display:grid;align-content:start;gap:10px}.lc-cards-cardhead{display:flex;align-items:center;gap:10px}.lc-cards-cardhead h2{margin:0}.lc-cards-invalidate{margin-left:auto;background:#7b2323!important;border-color:#c84b4b!important}.lc-cards-unpublish{background:#75460f!important;border-color:#c77b27!important}.lc-cards-remove{background:#7b2323!important;border-color:#c84b4b!important}.lc-cards-editing{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.lc-cards-editing.picture-rail-open{grid-template-columns:minmax(0,1fr) minmax(0,1fr) 180px}.lc-cards-fullpanel,.lc-cards-sectioncolumn{min-width:0;display:grid;align-content:start;gap:7px}.lc-cards-fullbar{display:flex;align-items:center;gap:8px}.lc-cards-fullbar h3{margin:0 auto 0 0}.lc-cards-full{box-sizing:border-box;white-space:pre-wrap;overflow:auto;height:430px;margin:0;background:#07131d;border:1px solid #223e52;border-radius:7px;padding:12px;font:12px ui-monospace,monospace;line-height:1.55}.lc-cards-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}.lc-cards-tab.active{background:#0968bd;border-color:#1684e6}.lc-cards-section{border:1px solid #223e52;background:#0b1b28;border-radius:7px;padding:12px;display:grid;gap:9px}.lc-cards-sectionbar{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.lc-cards-sectionbar strong{margin-right:auto}.lc-cards-text{width:100%;height:330px;resize:vertical;font:13px ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.55}.lc-cards-import .lc-cards-text{height:250px}.lc-cards-provenance{font-size:11px;color:#94a9b8}.lc-picture-rail{min-width:0;height:430px;overflow:auto;background:#07131d;border:1px solid #223e52;border-radius:7px;padding:8px;box-sizing:border-box}.lc-picture-rail-heading{display:grid;gap:2px;margin-bottom:8px}.lc-picture-rail-heading small{color:#91aabd}.lc-picture-rail-list{display:grid;gap:8px}.lc-picture-tile{display:grid;gap:3px;padding:7px;background:#0d2130;border:1px solid #29465b;border-radius:6px;overflow:hidden}.lc-picture-tile img,.lc-picture-placeholder{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:4px;background:#02070a}.lc-picture-placeholder{display:grid;place-items:center;color:#718898;font-weight:700}.lc-picture-tile span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lc-picture-tile small{color:#91aabd;line-height:1.25}.lc-picture-tile.inactive{opacity:.55;border-style:dashed}.lc-picture-toggle.active{background:#0968bd!important;border-color:#1684e6!important}
        .lc-cards-fields{display:grid;grid-template-columns:repeat(2,minmax(160px,1fr));gap:9px}.lc-cards-field{display:grid;gap:4px}.lc-cards-field label{color:#a9bdca}.lc-card-loras{grid-column:1/-1;border:1px solid #29465b;border-radius:6px;padding:9px;display:grid;gap:7px}.lc-card-lora-head{display:flex;align-items:center;gap:8px}.lc-card-lora-head strong{margin-right:auto}.lc-card-lora-list{display:grid;gap:6px}.lc-card-lora-row{display:grid;grid-template-columns:auto minmax(180px,1.4fr) 95px minmax(150px,1fr) auto;gap:6px;align-items:center}.lc-card-lora-row input[type=checkbox]{width:auto}.lc-card-lora-actions{display:flex;gap:4px}.lc-card-lora-actions button{padding:5px 7px}.lc-card-identity-overrides{display:grid;gap:5px}.lc-card-identity-override{display:grid;grid-template-columns:minmax(0,1fr) 150px;gap:8px;align-items:center}.lc-card-identity-override span{overflow:hidden;text-overflow:ellipsis}.lc-refine-radios{display:flex;gap:14px;align-items:center;min-height:32px}.lc-refine-radios label,.lc-refine-check{display:flex!important;align-items:center;gap:6px;color:#dce8f3!important}.lc-refine-radios input,.lc-refine-check input{width:auto;padding:0;margin:0}.lc-subject-refine{border:1px solid #29465b;border-radius:5px;padding:8px;display:grid;gap:6px}.lc-subject-refine-title{font-weight:600}.lc-cards-prompt{white-space:pre-wrap;max-height:240px;overflow:auto;background:#07131d;padding:10px;border-radius:5px;font:11px ui-monospace,monospace}.lc-legacy{border-color:#a76b1b;background:#2b2112}.lc-legacy pre{white-space:pre-wrap;max-height:280px;overflow:auto}
        .lc-cards-preview h3{margin:12px 0 8px}.lc-cards-preview h3:first-child{margin-top:4px}.lc-cards-video{width:100%;max-height:280px;background:#02070a;border-radius:6px}.lc-cards-info,.lc-cards-assets{display:grid;gap:7px;margin-top:10px}.lc-cards-info>div,.lc-cards-asset{background:#0d2130;border:1px solid #223f53;border-radius:5px;padding:8px;word-break:break-word}.lc-cards-asset{display:grid;grid-template-columns:48px 1fr auto;gap:8px;align-items:center}.lc-cards-asset img{width:48px;height:48px;object-fit:cover;border-radius:4px;background:#02070a}.lc-cards-asset small{color:#91aabd}.lc-cards-help{color:#91aabd;font-size:11px;line-height:1.4}.lc-cards-identity-create{display:grid;grid-template-columns:1fr 90px;gap:6px}.lc-cards-identity-create .wide{grid-column:1/-1}.lc-cards-actions{display:flex;gap:8px;margin-left:auto}.lc-cards-execution{display:grid;grid-template-columns:auto minmax(120px,1fr) minmax(160px,320px) auto auto;gap:8px 12px;align-items:center;padding:8px 14px;background:#0a1925;border-top:1px solid #254052}.lc-cards-execution-state{font-weight:700;text-transform:uppercase;color:#91aabd;min-width:86px}.lc-cards-execution-state.running{color:#53aaff}.lc-cards-execution-state.completed{color:#35d181}.lc-cards-execution-state.failed,.lc-cards-execution-state.interrupted{color:#ff7b7b}.lc-cards-execution progress{width:100%;height:14px;accent-color:#1684e6}.lc-cards-execution-stage{color:#b7cbd8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lc-cards-execution-percent{font:12px ui-monospace,monospace;min-width:46px;text-align:right}.lc-cards-log{grid-column:1/-1;box-sizing:border-box;margin:0;max-height:132px;overflow:auto;white-space:pre-wrap;background:#050d14;border:1px solid #20394b;border-radius:5px;padding:7px 9px;color:#aebfca;font:11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}.lc-cards-execution.collapsed .lc-cards-log{display:none}.lc-log-error{color:#ff8a8a}.lc-log-warning{color:#f5bd4d}.lc-cards-footer{display:flex;padding:7px 14px;background:#0c1c29;border-top:1px solid #254052;color:#9bb0bf}.lc-cards-save{margin-left:auto}.lc-cards-error{color:#ff7b7b}.lc-cards-dirty{color:#f5bd4d}@media(max-width:1250px){.lc-cards-main{grid-template-columns:220px 1fr}.lc-cards-preview{display:none}.lc-cards-project{grid-template-columns:1fr}.lc-cards-new{grid-template-columns:repeat(4,1fr)}.lc-cards-editing{grid-template-columns:1fr}}
        .lc-cards-asset.in-use{background:#103b2d;border-color:#35b779;box-shadow:inset 4px 0 #35d181}.lc-cards-asset.in-use small{color:#b9ead2}.lc-identity-controls{display:flex;gap:6px;align-items:center}.lc-identity-delete{background:#6f2530!important;border-color:#ba4654!important}.lc-project-lora{display:grid;gap:8px}.lc-project-lora textarea{min-height:90px;resize:vertical;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}.lc-project-lora-actions{display:flex;align-items:center;gap:8px}.lc-project-templates{display:grid;gap:8px}.lc-template-list{display:grid;gap:8px}.lc-template-row{display:grid;grid-template-columns:minmax(130px,.45fr) minmax(240px,1fr) auto;gap:7px;align-items:start}.lc-template-row input,.lc-template-row textarea{box-sizing:border-box;width:100%;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}.lc-template-row textarea{min-height:72px;resize:vertical}.lc-template-actions{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.lc-template-preview{max-height:260px;overflow:auto;white-space:pre-wrap;background:#07131d;border:1px solid #223e52;border-radius:5px;padding:9px;font:11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}.lc-template-warning{color:#f5bd4d}.lc-identity-import{grid-column:1/-1;box-sizing:border-box;min-height:76px;border:2px dashed #31546b;border-radius:7px;display:grid;place-items:center;padding:12px;color:#9fc0d4;background:#081520;cursor:pointer;text-align:center}.lc-identity-import:hover,.lc-identity-import.dragging{border-color:#1684e6;background:#0c2537;color:#d8edfa}.lc-identity-import.disabled{opacity:.5;cursor:not-allowed}.lc-identity-import input{display:none}.lc-identity-import small{display:block;margin-top:4px;color:#7897aa}
        .lc-project-resolution{display:grid;gap:9px}.lc-project-resolution-fields{display:grid;grid-template-columns:minmax(180px,1fr) minmax(130px,.6fr) minmax(190px,1fr);gap:8px}.lc-project-resolution-fields label{display:grid;gap:4px;color:#a9bdca}.lc-project-resolution-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.lc-project-resolution-lock{color:#f5bd4d}
        .lc-draft-takes{display:grid;gap:6px}.lc-draft-take{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px;align-items:center;background:#0d2130;border:1px solid #223f53;border-radius:5px;padding:8px}.lc-draft-take.selected{background:#103b2d;border-color:#35b779}.lc-draft-take.viewed{box-shadow:inset 3px 0 #1684e6}.lc-draft-take-meta{display:grid;gap:2px;min-width:0}.lc-draft-take-meta small{color:#91aabd}.lc-draft-take-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}.lc-draft-take-actions button{padding:5px 7px}.lc-draft-takes-head{display:flex;align-items:center;gap:8px}.lc-draft-takes-head h3{margin-right:auto}
        .lc-prompt-diff-details{margin-top:8px;border:1px solid #29465b;border-radius:5px;background:#07131d}.lc-prompt-diff-details summary{cursor:pointer;padding:8px;color:#b9cbd7}.lc-prompt-diff{max-height:420px;overflow:auto;border-top:1px solid #223f53;font:11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}.lc-prompt-diff-row{display:grid;grid-template-columns:42px 42px 18px minmax(0,1fr);white-space:pre-wrap;word-break:break-word;padding:1px 6px}.lc-prompt-diff-row .line-number{color:#607b8d;text-align:right;padding-right:7px;user-select:none}.lc-prompt-diff-row.add{background:#103b2d;color:#c8f2dc}.lc-prompt-diff-row.delete{background:#451f27;color:#ffd0d4}.lc-prompt-diff-empty{padding:9px;color:#91aabd}
        .lc-generation-setup{border-top:1px solid #223f53;padding:8px;display:grid;gap:8px}.lc-generation-group{display:grid;gap:4px}.lc-generation-group>strong{color:#9fc2d8}.lc-generation-item{background:#0d2130;border:1px solid #223f53;border-radius:4px;padding:6px;display:grid;gap:2px}.lc-generation-item small{color:#91aabd;white-space:pre-wrap;word-break:break-word}.lc-generation-stats{color:#91aabd;font-size:11px}.lc-generation-warning{color:#f5bd4d}
        .lc-cards-log{height:132px;min-height:0;overflow-y:auto;overflow-x:auto;overscroll-behavior:contain;scrollbar-gutter:stable}
        @media(max-width:1450px){.lc-cards-editing.picture-rail-open{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.lc-picture-rail{grid-column:1/-1;height:auto;max-height:240px}.lc-picture-rail-list{grid-template-columns:repeat(auto-fill,minmax(120px,1fr))}}
        @media(max-width:1250px){.lc-cards-editing.picture-rail-open{grid-template-columns:1fr}.lc-picture-rail{grid-column:auto}}
    `;
    document.head.append(style);

    const top = cardsElement("div", "lc-cards-top");
    top.append(cardsElement("div", "lc-cards-brand", "LongCaster Studio"));
    const projectSelect = document.createElement("select");
    const refresh = cardsElement("button", "", "Refresh");
    const newProject = cardsElement("button", "", "New Project");
    const duplicateProject = cardsElement("button", "", "Duplicate Project");
    const projectPath = cardsElement("span", "lc-cards-projectpath");
    const actions = cardsElement("div", "lc-cards-actions");
    const generate = cardsElement("button", "lc-cards-primary", "Generate Draft");
    const draftCount = document.createElement("input");
    draftCount.type = "number"; draftCount.min = "1"; draftCount.max = "100"; draftCount.step = "1"; draftCount.value = "1";
    draftCount.style.width = "64px"; draftCount.title = "Number of drafts to queue (1–100)";
    draftCount.setAttribute("aria-label", "Draft count");
    const queueDrafts = cardsElement("button", "lc-cards-primary", "Queue Drafts");
    queueDrafts.title = "Generate the selected number of draft takes with different seeds. Keep Studio open until all takes finish.";
    const retry = cardsElement("button", "", "Retry Draft");
    const retryDifferentSeed = cardsElement("button", "", "Retry Draft with Different Seed");
    const accept = cardsElement("button", "lc-cards-accept", "Accept Draft");
    const batchRender = cardsElement("button", "lc-cards-primary", "Render Remaining Drafts");
    const append = cardsElement("button", "lc-cards-primary", "Append Card");
    actions.append(generate, draftCount, queueDrafts, retry, retryDifferentSeed, accept, batchRender, append);
    const stop = cardsElement("button", "lc-cards-danger", "Stop Generation / Unlock");
    const close = cardsElement("button", "lc-cards-close", "Close");
    top.append(projectSelect, refresh, newProject, duplicateProject, projectPath, actions, stop, close);

    const newPanel = cardsElement("div", "lc-cards-new"); newPanel.hidden = true;
    const newName = document.createElement("input"); newName.placeholder = "my_project";
    const newMode = document.createElement("select");
    for (const value of ["ref2va", "t2va"]) { const option = document.createElement("option"); option.value = value; option.textContent = value.toUpperCase(); newMode.append(option); }
    const initialWidth = Number(widget(node, "width")?.value || 544);
    const initialHeight = Number(widget(node, "height")?.value || 960);
    const newAspect = document.createElement("select");
    for (const [value] of PROJECT_ASPECT_RATIOS) { const option = document.createElement("option"); option.value = value; option.textContent = value; newAspect.append(option); }
    newAspect.value = nearestProjectAspect(initialWidth, initialHeight);
    const newMegapixels = document.createElement("input"); newMegapixels.type = "number"; newMegapixels.min = "0.1"; newMegapixels.max = "16"; newMegapixels.step = "0.1"; newMegapixels.value = String(Math.min(16, Math.max(0.1, Math.round(initialWidth * initialHeight / (1024 * 1024) * 10) / 10)));
    const newResolution = cardsElement("div", "lc-cards-resolution");
    const selectedNewResolution = () => projectResolution(newAspect.value, newMegapixels.value);
    const renderNewResolution = () => { const value = selectedNewResolution(); newResolution.textContent = `${value.width} × ${value.height} · multiple of ${PROJECT_RESOLUTION_MULTIPLE}`; };
    newAspect.onchange = renderNewResolution; newMegapixels.oninput = renderNewResolution; renderNewResolution();
    const newDuration = document.createElement("input"); newDuration.type = "number"; newDuration.min = ".1"; newDuration.max = "120"; newDuration.step = ".1"; newDuration.value = String(widget(node, "duration_seconds")?.value || 5);
    const newSeed = document.createElement("input"); newSeed.type = "text"; newSeed.value = String(widget(node, "seed")?.value || 0);
    const createProject = cardsElement("button", "lc-cards-primary", "Create");
    const cancelCreate = cardsElement("button", "", "Cancel");
    const field = (label, input) => { const wrapper = document.createElement("label"); wrapper.append(document.createTextNode(label), input); return wrapper; };
    newPanel.append(field("Project folder name", newName), field("Generation mode", newMode), field("Aspect ratio", newAspect), field("Megapixels (MP)", newMegapixels), field("Resolved size (×32)", newResolution), field("First-card seconds", newDuration), field("Seed", newSeed), createProject, cancelCreate);

    const studioTabs = cardsElement("nav", "lc-studio-tabs");
    const cardViewTab = cardsElement("button", "lc-studio-tab active", "Card");
    const projectViewTab = cardsElement("button", "lc-studio-tab", "Project");
    studioTabs.append(cardViewTab, projectViewTab);

    const executionPanel = cardsElement("section", "lc-cards-execution");
    const executionState = cardsElement("span", "lc-cards-execution-state", "Idle");
    const executionStage = cardsElement("span", "lc-cards-execution-stage", "No LongCaster operation is running.");
    const executionProgress = document.createElement("progress"); executionProgress.max = 1; executionProgress.value = 0;
    const executionPercent = cardsElement("span", "lc-cards-execution-percent", "0%");
    const executionToggle = cardsElement("button", "", "Hide Execution Log");
    const executionLog = cardsElement("pre", "lc-cards-log", "LongCaster execution log ready.");
    executionLog.tabIndex = 0;
    executionLog.setAttribute("role", "log");
    executionLog.setAttribute("aria-live", "polite");
    executionPanel.append(executionState, executionStage, executionProgress, executionPercent, executionToggle, executionLog);

    const main = cardsElement("div", "lc-cards-main");
    const sidebar = cardsElement("aside", "lc-cards-sidebar");
    const list = cardsElement("div", "lc-cards-list");
    sidebar.append(list);
    const editor = cardsElement("main", "lc-cards-editor");
    const cardHead = cardsElement("div", "lc-cards-cardhead");
    const previousCard = cardsElement("button", "", "‹");
    const cardTitle = cardsElement("h2", "", "Card");
    const nextCard = cardsElement("button", "", "›");
    const cardStatus = cardsElement("span", "");
    const invalidateRender = cardsElement("button", "lc-cards-invalidate", "Invalidate From This Card");
    const unpublish = cardsElement("button", "lc-cards-unpublish", "Unpublish to Draft");
    const removeDraft = cardsElement("button", "lc-cards-remove", "Remove Draft Card");
    cardHead.append(previousCard, cardTitle, nextCard, cardStatus, invalidateRender, unpublish, removeDraft);
    const tabs = cardsElement("div", "lc-cards-tabs");
    const section = cardsElement("section", "lc-cards-section");
    const sectionBar = cardsElement("div", "lc-cards-sectionbar");
    const sectionName = document.createElement("strong");
    const copyPrevious = cardsElement("button", "", "Copy Previous");
    const sourceSelect = document.createElement("select");
    const copySelected = cardsElement("button", "", "Copy From Card");
    const clearSection = cardsElement("button", "", "Clear");
    sectionBar.append(sectionName, copyPrevious, sourceSelect, copySelected, clearSection);
    const textarea = cardsElement("textarea", "lc-cards-text");
    const provenance = cardsElement("div", "lc-cards-provenance");
    section.append(sectionBar, textarea, provenance);
    const legacy = cardsElement("section", "lc-cards-section lc-legacy"); legacy.hidden = true;
    const legacyMessage = cardsElement("div", "", "This historical card has a flat prompt. It remains unchanged until you explicitly convert it.");
    const legacyPrompt = document.createElement("pre");
    const convertLegacy = cardsElement("button", "", "Convert to Sections");
    legacy.append(legacyMessage, legacyPrompt, convertLegacy);
    const fields = cardsElement("div", "lc-cards-fields");
    const durationField = cardsElement("div", "lc-cards-field");
    const duration = document.createElement("input"); duration.type = "number"; duration.min = "0.1"; duration.max = "120"; duration.step = "0.1";
    durationField.append(cardsElement("label", "", "Duration (seconds)"), duration);
    const seedField = cardsElement("div", "lc-cards-field");
    const seed = document.createElement("input"); seed.type = "text"; seed.inputMode = "numeric";
    seedField.append(cardsElement("label", "", "Seed"), seed);
    const modeField = cardsElement("div", "lc-cards-field");
    const mode = document.createElement("select");
    for (const value of ["ref2va", "t2va"]) { const option = document.createElement("option"); option.value = value; option.textContent = value.toUpperCase(); mode.append(option); }
    modeField.append(cardsElement("label", "", "Generation mode"), mode);
    modeField.append(cardsElement("div", "lc-cards-help", "REF2VA uses the connected reference packet on every card. T2VA is text-only. Mode locks after the first render."));
    const strategyField = cardsElement("div", "lc-cards-field");
    const strategy = document.createElement("select");
    for (const [value, label] of [["direct_mmh3", "Continue previous card (direct MMH3)"], ["image_restart", "Image restart (previous last frame)"], ["independent", "Independent shot"]]) { const option = document.createElement("option"); option.value = value; option.textContent = label; strategy.append(option); }
    strategyField.append(cardsElement("label", "", "Continuation strategy"), strategy);
    strategyField.append(cardsElement("div", "lc-cards-help", "Direct MMH3 preserves the accepted parent’s final 39 joint audio/video latent frames. Image Restart creates a fresh latent and guides frame 0 with the predecessor’s final image. Independent starts without a generation parent."));
    const treatmentField = cardsElement("div", "lc-cards-field");
    const treatment = document.createElement("select");
    for (const [value, label] of [["off", "Off"], ["drift_control", "Drift-Control AV"], ["color_stable", "Drift + Color-Stable AV"]]) {
        const option = document.createElement("option"); option.value = value; option.textContent = label; treatment.append(option);
    }
    treatmentField.append(cardsElement("label", "", "Continuation treatment"), treatment);
    treatmentField.append(cardsElement("div", "lc-cards-help", "Project-level policy for direct MMH3 continuation. It runs independently of optional source refinement. Existing projects default to Off."));
    const colorBaselineField = cardsElement("div", "lc-cards-field");
    const colorBaseline = document.createElement("select");
    colorBaselineField.append(cardsElement("label", "", "Color-Stable baseline card"), colorBaseline);
    colorBaselineField.append(cardsElement("div", "lc-cards-help", "Choose any earlier accepted card as this direct continuation's scene-color baseline. The choice is inherited by newly appended cards until you change it."));
    const identityOverridesField = cardsElement("div", "lc-cards-field");
    const identityOverrides = cardsElement("div", "lc-card-identity-overrides");
    identityOverridesField.append(
        cardsElement("label", "", "Identity anchors for this card"), identityOverrides,
        cardsElement("div", "lc-cards-help", "Project follows each subject's project-level Enabled/Disabled state. Enabled and Disabled override it only for this card."),
    );
    const refImageSizeField = cardsElement("div", "lc-cards-field");
    const refImageSize = document.createElement("select");
    for (const [value, label] of [["match", "Match generation size"], ["max", "Maximum reference size"]]) {
        const option = document.createElement("option"); option.value = value; option.textContent = label; refImageSize.append(option);
    }
    refImageSizeField.append(cardsElement("label", "", "REF2VA reference image size"), refImageSize);
    refImageSizeField.append(cardsElement("div", "lc-cards-help", "Saved per card. Retry uses this card's current choice; each completed take freezes the value it used."));
    const makeRefineRadios = (suffix) => {
        const root = cardsElement("div", "lc-refine-radios");
        const inputs = {};
        for (const [value, label] of [["off", "Off"], ["every_accepted_card", "Auto"], ["manual", "Manual"]]) {
            const input = document.createElement("input"); input.type = "radio"; input.name = `lc-refine-${node.id}-${suffix}`; input.value = value;
            const wrapper = document.createElement("label"); wrapper.append(input, document.createTextNode(label)); root.append(wrapper); inputs[value] = input;
        }
        return {root, inputs};
    };
    const setRefineRadios = (group, value) => { for (const input of Object.values(group.inputs)) input.checked = input.value === value; };
    const disableRefineRadios = (group, disabled) => { for (const input of Object.values(group.inputs)) input.disabled = disabled; };
    const refineCadenceField = cardsElement("div", "lc-cards-field");
    const refineCadence = makeRefineRadios("card");
    refineCadenceField.append(cardsElement("label", "", "Optional source refine"), refineCadence.root);
    refineCadenceField.append(cardsElement("div", "lc-cards-help", "Optionally creates a same-resolution continuation source. This does not disable or replace Drift + Color-Stable, and the accepted master is never replaced."));
    const refineEnabledField = cardsElement("div", "lc-cards-field");
    const refineEnabled = document.createElement("input"); refineEnabled.type = "checkbox";
    const refineEnabledLabel = cardsElement("label", "lc-refine-check"); refineEnabledLabel.append(refineEnabled, document.createTextNode("Create refined source after accepting this card"));
    refineEnabledField.append(refineEnabledLabel, cardsElement("div", "lc-cards-help", "Used only in Manual mode. The setting becomes read-only after acceptance."));
    const cardLoraField = cardsElement("section", "lc-card-loras");
    const cardLoraHead = cardsElement("div", "lc-card-lora-head");
    const addCardLora = cardsElement("button", "", "Add LoRA");
    const refreshCardLoras = cardsElement("button", "", "Refresh List");
    const cardLoraList = cardsElement("div", "lc-card-lora-list");
    cardLoraHead.append(cardsElement("strong", "", "Card LoRAs"), addCardLora, refreshCardLoras);
    cardLoraField.append(
        cardLoraHead,
        cardsElement("div", "lc-cards-help", "Applied in order by the LongCaster Card LoRAs node before ModelSampling and PDD. Activation words are injected only for enabled non-zero entries."),
        cardLoraList,
    );
    fields.append(durationField, seedField, modeField, strategyField, treatmentField, colorBaselineField, identityOverridesField, refImageSizeField, refineCadenceField, refineEnabledField, cardLoraField);
    const cardReferenceControls = createCardReferenceControls({
        assetURL: path => api.apiURL(path),
        onSettings(settings) {
            Object.assign(selectedCard().card_reference, settings);
            markDirty();
            renderCardReference();
            renderPictureRail(selectedCard(), viewedTake());
        },
        onUpload: (file) => changeCardReference(file),
        onRemove: () => changeCardReference(null),
    });
    fields.append(cardReferenceControls.root);
    const subjectRefine = cardsElement("div", "lc-subject-refine");
    const subjectRefineCadence = makeRefineRadios("subject");
    const subjectRefineEnabled = document.createElement("input"); subjectRefineEnabled.type = "checkbox";
    const subjectRefineEnabledLabel = cardsElement("label", "lc-refine-check"); subjectRefineEnabledLabel.append(subjectRefineEnabled, document.createTextNode("Create refined source after accepting this card"));
    subjectRefine.append(cardsElement("div", "lc-subject-refine-title", "Optional source refine"), subjectRefineCadence.root, subjectRefineEnabledLabel, cardsElement("div", "lc-cards-help", "Controls only creation of an optional continuation source. It is independent of Drift + Color-Stable. Off never creates one; Auto creates one after every accepted card; Manual follows this card checkbox."));
    section.insertBefore(subjectRefine, textarea);
    const editing = cardsElement("div", "lc-cards-editing");
    const fullPanel = cardsElement("div", "lc-cards-fullpanel");
    const fullBar = cardsElement("div", "lc-cards-fullbar");
    const importFull = cardsElement("button", "", "Paste Full Prompt");
    const pictureToggle = cardsElement("button", "lc-picture-toggle", "Pictures");
    pictureToggle.type = "button";
    fullBar.append(cardsElement("h3", "", "Full Prompt"), pictureToggle, importFull);
    const fullPrompt = cardsElement("pre", "lc-cards-full");
    const importPanel = cardsElement("section", "lc-cards-section lc-cards-import"); importPanel.hidden = true;
    const importHelp = cardsElement("div", "lc-cards-help", "Paste the plain MiniMax H3 prompt beginning with subject_definitions:. Canonical headings are assigned automatically; unlabelled text goes into Detailed Description. XML is not required.");
    const importText = cardsElement("textarea", "lc-cards-text");
    importText.placeholder = "Paste the full prompt here…";
    const importActions = cardsElement("div", "lc-cards-sectionbar");
    const applyImport = cardsElement("button", "lc-cards-primary", "Replace and Split into Sections");
    const cancelImport = cardsElement("button", "", "Cancel");
    importActions.append(applyImport, cancelImport);
    importPanel.append(importHelp, importText, importActions);
    fullPanel.append(fullBar, fullPrompt, importPanel);
    const sectionColumn = cardsElement("div", "lc-cards-sectioncolumn");
    sectionColumn.append(tabs, section);
    const pictureRail = createPictureReferenceRail();
    pictureRail.root.id = `lc-picture-rail-${node.id}`;
    pictureToggle.setAttribute("aria-controls", pictureRail.root.id);
    editing.append(fullPanel, sectionColumn, pictureRail.root);
    editor.append(cardHead, editing, legacy, fields);

    const preview = cardsElement("aside", "lc-cards-preview");
    preview.append(cardsElement("h3", "", "Card Preview"));
    const video = cardsElement("video", "lc-cards-video"); video.controls = true; video.preload = "metadata";
    const buildPreview = cardsElement("button", "", "Generate missing preview");
    const takesHeading = cardsElement("div", "lc-draft-takes-head");
    const deleteUnselectedTakes = cardsElement("button", "lc-identity-delete", "Delete Unselected");
    takesHeading.append(cardsElement("h3", "", "Draft Takes"), deleteUnselectedTakes);
    const draftTakes = cardsElement("div", "lc-draft-takes");
    const promptDiffDetails = document.createElement("details"); promptDiffDetails.className = "lc-prompt-diff-details";
    const promptDiffSummary = document.createElement("summary"); promptDiffSummary.textContent = "Prompt Diff";
    const promptDiff = cardsElement("div", "lc-prompt-diff");
    promptDiffDetails.append(promptDiffSummary, promptDiff);
    const generationSetupDetails = document.createElement("details"); generationSetupDetails.className = "lc-prompt-diff-details";
    const generationSetupSummary = document.createElement("summary"); generationSetupSummary.textContent = "Take Settings / Generation Setup";
    const generationSetup = cardsElement("div", "lc-generation-setup");
    generationSetupDetails.append(generationSetupSummary, generationSetup);
    const info = cardsElement("div", "lc-cards-info");
    const refineStatus = cardsElement("div", "lc-cards-info");
    const createRefine = cardsElement("button", "", "Generate Refine");
    const useAcceptedMaster = cardsElement("button", "", "Use Accepted Master");
    const useRefined = cardsElement("button", "", "Use Refined");
    const refineControls = cardsElement("div", "lc-cards-actions");
    refineControls.append(createRefine, useAcceptedMaster, useRefined);
    const identities = cardsElement("div", "lc-cards-assets");
    const references = cardsElement("div", "lc-cards-assets");
    const editReferences = cardsElement("button", "", "Edit connected reference graph");
    const identityHeading = cardsElement("div", "lc-cards-fullbar");
    const refreshIdentities = cardsElement("button", "", "Refresh");
    identityHeading.append(cardsElement("h3", "", "Identity Checkpoints"), refreshIdentities);
    const referenceHeading = cardsElement("div", "lc-cards-fullbar");
    const refreshReferences = cardsElement("button", "", "Refresh");
    referenceHeading.append(cardsElement("h3", "", "Reference Resources"), refreshReferences);
    const identitySubject = document.createElement("input"); identitySubject.value = "<Subject 1>"; identitySubject.placeholder = "<Subject 1>";
    const identityFrame = document.createElement("input"); identityFrame.type = "number"; identityFrame.min = "0"; identityFrame.value = "0";
    const identityLabel = document.createElement("input"); identityLabel.value = "identity checkpoint"; identityLabel.placeholder = "Identity label"; identityLabel.className = "wide";
    const identityScope = document.createElement("select"); identityScope.className = "wide";
    for (const value of ["face_only", "face_clothing", "face_body", "everything", "custom"]) { const option = document.createElement("option"); option.value = value; option.textContent = value.replaceAll("_", " "); identityScope.append(option); }
    const identityCustom = document.createElement("textarea"); identityCustom.className = "wide"; identityCustom.placeholder = "For custom scope, describe exactly what this image may contribute."; identityCustom.hidden = true;
    const createIdentity = cardsElement("button", "wide", "Use selected card frame as identity");
    const identityImportInput = document.createElement("input"); identityImportInput.type = "file"; identityImportInput.accept = "image/*";
    const identityImport = cardsElement("div", "lc-identity-import", "Drop an identity image here or click to browse");
    identityImport.tabIndex = 0;
    identityImport.append(cardsElement("small", "", "The image is copied into this project and selected for the subject above."), identityImportInput);
    preview.append(video, buildPreview, takesHeading, draftTakes, promptDiffDetails, generationSetupDetails, info, cardsElement("h3", "", "Continuation Refine"), refineStatus, refineControls);
    main.append(sidebar, editor, preview);

    const projectView = cardsElement("section", "lc-cards-project"); projectView.hidden = true;
    const resolutionPanel = cardsElement("section", "lc-project-panel lc-project-resolution");
    const projectAspect = document.createElement("select");
    for (const [value] of PROJECT_ASPECT_RATIOS) { const option = document.createElement("option"); option.value = value; option.textContent = value; projectAspect.append(option); }
    const projectMegapixels = document.createElement("input"); projectMegapixels.type = "number"; projectMegapixels.min = "0.1"; projectMegapixels.max = "16"; projectMegapixels.step = "0.05";
    const projectResolved = cardsElement("div", "lc-cards-resolution");
    const resolutionFields = cardsElement("div", "lc-project-resolution-fields");
    resolutionFields.append(field("Aspect ratio", projectAspect), field("Megapixels (MP)", projectMegapixels), field("Resolved size (×32)", projectResolved));
    const saveProjectResolution = cardsElement("button", "lc-cards-primary", "Save Resolution");
    const invalidateAllRenders = cardsElement("button", "lc-cards-danger", "Invalidate All Renders");
    const duplicateAndInvalidate = cardsElement("button", "", "Duplicate and Invalidate Project");
    const resolutionLock = cardsElement("span", "lc-project-resolution-lock", "To change resolution, invalidate all cards first.");
    const resolutionActions = cardsElement("div", "lc-project-resolution-actions");
    resolutionActions.append(saveProjectResolution, invalidateAllRenders, duplicateAndInvalidate, resolutionLock);
    resolutionPanel.append(
        cardsElement("h2", "", "Project Resolution"),
        cardsElement("div", "lc-cards-help", "Resolution applies to every regenerated card in this project."),
        resolutionFields,
        resolutionActions,
    );
    const identityPanel = cardsElement("section", "lc-project-panel");
    identityPanel.append(cardsElement("h2", "", "Identity Anchors"), cardsElement("div", "lc-cards-help", "Inspect, activate, disable, or clear saved identities. Create one from an accepted card frame or import an arbitrary image."));
    const identitySource = cardsElement("div", "lc-project-identity-source");
    const identitySourceSelect = document.createElement("select");
    const buildIdentityPreview = cardsElement("button", "", "Generate Missing Preview");
    const identityVideo = cardsElement("video", "lc-project-identity-video"); identityVideo.controls = true; identityVideo.preload = "metadata";
    const identityForm = cardsElement("div", "lc-project-identity-form");
    identityCustom.hidden = false;
    const customScopeField = field("Custom scope instruction", identityCustom); customScopeField.className = "wide"; customScopeField.hidden = true;
    createIdentity.className = "wide";
    const identityFrameField = field("Frame", identityFrame);
    identityForm.append(
        field("Subject", identitySubject), identityFrameField, field("Label", identityLabel),
        field("Scope", identityScope), customScopeField, createIdentity,
        identityImport,
    );
    identitySource.append(field("Accepted source card", identitySourceSelect), buildIdentityPreview, identityVideo, identityForm);
    identityPanel.append(identityHeading, identities, identitySource);
    const referencePanel = cardsElement("section", "lc-project-panel");
    referencePanel.append(cardsElement("h2", "", "Project References"), cardsElement("div", "lc-cards-help", "Current connected references and the latest recorded card snapshot."), referenceHeading, references, editReferences);
    const loraPanel = cardsElement("section", "lc-project-panel lc-project-lora");
    const loraWords = document.createElement("textarea"); loraWords.maxLength = 2000; loraWords.placeholder = "Exact LoRA activation words, preserving case and punctuation";
    const saveLoraWords = cardsElement("button", "lc-cards-primary", "Save Activation Words");
    const loraActions = cardsElement("div", "lc-project-lora-actions");
    loraActions.append(saveLoraWords, cardsElement("span", "lc-cards-help", "Changes to an existing draft require Retry before Accept."));
    loraPanel.append(
        cardsElement("h2", "", "Project LoRA Activation Words"),
        cardsElement("div", "lc-cards-help", "Injected at runtime at the start of subject_definitions for every newly generated card. This does not load a LoRA; apply the matching LoRA to the connected model graph."),
        loraWords,
        loraActions,
    );
    const templatePanel = cardsElement("section", "lc-project-panel lc-project-templates");
    const templateList = cardsElement("div", "lc-template-list");
    const addTemplate = cardsElement("button", "", "Add Template");
    const saveTemplates = cardsElement("button", "lc-cards-primary", "Save Templates");
    const templateActions = cardsElement("div", "lc-template-actions");
    const templateWarnings = cardsElement("div", "lc-template-warning");
    const templatePreview = cardsElement("pre", "lc-template-preview");
    templateActions.append(addTemplate, saveTemplates, cardsElement("span", "lc-cards-help", "Use the exact name in prompts, for example <lighting> or <sound design>."));
    templatePanel.append(
        cardsElement("h2", "", "Project Prompt Templates"),
        cardsElement("div", "lc-cards-help", "Define reusable project text. Templates are user-created and expand at generation time without changing saved card prompts. MiniMax reference labels remain reserved."),
        templateList,
        templateActions,
        templateWarnings,
        cardsElement("h3", "", "Selected Card Resolved Preview"),
        templatePreview,
    );
    const promptExportPanel = cardsElement("section", "lc-project-panel");
    const exportProjectPrompts = cardsElement("button", "lc-cards-primary", "Export Project Prompts");
    promptExportPanel.append(
        cardsElement("h2", "", "Prompt Export"),
        cardsElement("div", "lc-cards-help", "Download every card's selected prompt in timeline order. Prompts are separated by --- lines."),
        exportProjectPrompts,
    );
    const projectPreviewPanel = cardsElement("section", "lc-project-panel");
    const playProjectPreview = cardsElement("button", "lc-cards-primary", "Play Project Preview");
    const projectPreviewStatus = cardsElement("div", "lc-cards-help", "Plays each selected card preview in timeline order without creating a stitched file.");
    const projectPreviewVideo = cardsElement("video", "lc-project-identity-video");
    projectPreviewVideo.controls = true; projectPreviewVideo.preload = "metadata"; projectPreviewVideo.hidden = true;
    projectPreviewPanel.append(
        cardsElement("h2", "", "Project Preview"),
        projectPreviewStatus,
        playProjectPreview,
        projectPreviewVideo,
    );
    projectView.append(resolutionPanel, loraPanel, templatePanel, identityPanel, referencePanel, promptExportPanel, projectPreviewPanel);
    const footer = cardsElement("div", "lc-cards-footer");
    const message = cardsElement("span", "", "Loading…");
    const saveState = cardsElement("span", "lc-cards-save");
    footer.append(message, saveState);
    const studioNav = cardsElement("div", "lc-studio-nav");
    studioNav.append(newPanel, studioTabs);
    root.append(top, studioNav, main, projectView, executionPanel, footer);
    document.body.append(root);

    let pictureRailOpen = true;
    try { pictureRailOpen = globalThis.localStorage?.getItem("longcaster.pictureRailOpen") !== "false"; } catch (_) {}
    const workspace = {
        root, node, state: null, selectedCardId: null, selectedSection: CARD_SECTIONS[0][0],
        dirty: false, dirtySections: new Set(), timer: null, saving: null, editVersion: 0,
        loraDirty: false, templatesDirty: false, templateDraft: [],
        followActive: false, loadEpoch: 0, projectListEpoch: 0, importCardId: null,
        activeView: "card", identitySourceCardId: null, viewedTakeId: null,
        pictureRailOpen, batch: null, draftBatch: null, projectPreviewClips: [], projectPreviewIndex: -1,
        availableLoras: [], loraCatalogLoaded: false,
    };
    node.longcasterCardsWorkspace = workspace;
    node.graph.longcasterProjectAuthority = node;

    const selectedCard = () => workspace.state?.cards?.find((card) => card.id === workspace.selectedCardId);
    const viewedTake = (card = selectedCard()) => card?.draft_takes?.find((take) => take.id === workspace.viewedTakeId);
    const identitySourceCard = () => workspace.state?.cards?.find((card) => card.id === workspace.identitySourceCardId && card.status === "ACCEPTED");
    const editable = (card) => Boolean(card && !workspace.batch?.active && !workspace.referenceBusy && !workspace.state?.pending_operation && card.id === workspace.state?.active_card_id && ["EMPTY", "DRAFT", "FAILED", "INVALIDATED"].includes(card.status));
    const setMessage = (value, error = false) => { message.textContent = value; message.className = error ? "lc-cards-error" : ""; };
    const selectedProjectResolution = () => projectResolution(projectAspect.value, projectMegapixels.value);
    const renderProjectResolutionChoice = () => {
        const value = selectedProjectResolution();
        projectResolved.textContent = `${value.width} × ${value.height} · multiple of ${PROJECT_RESOLUTION_MULTIPLE}`;
        const state = workspace.state;
        saveProjectResolution.disabled = !state?.resolution_editable
            || Boolean(state?.pending_operation)
            || (value.width === state.width && value.height === state.height);
    };
    const scrollExecutionLogToEnd = () => {
        const apply = () => { executionLog.scrollTop = executionLog.scrollHeight; };
        if (typeof globalThis.requestAnimationFrame === "function") globalThis.requestAnimationFrame(apply);
        else setTimeout(apply, 0);
    };
    const execution = {
        active: false,
        action: null,
        promptId: null,
        ignoredPromptIds: new Set(),
        entries: [],
        render(status, stage, value = executionProgress.value, maximum = executionProgress.max) {
            const safeMaximum = Math.max(1, Number(maximum) || 1);
            const safeValue = Math.max(0, Math.min(safeMaximum, Number(value) || 0));
            executionState.textContent = status;
            executionState.className = `lc-cards-execution-state ${status.toLowerCase()}`;
            executionStage.textContent = stage;
            executionProgress.max = safeMaximum;
            executionProgress.value = safeValue;
            executionPercent.textContent = `${Math.round(safeValue / safeMaximum * 100)}%`;
        },
        append(level, text, timestamp = Date.now()) {
            const instant = new Date(Number(timestamp) < 100000000000 ? Number(timestamp) * 1000 : Number(timestamp));
            const time = Number.isNaN(instant.getTime()) ? new Date().toLocaleTimeString() : instant.toLocaleTimeString();
            const messageText = String(text || "").trim();
            if (!messageText) return;
            this.entries.push(`[${time}] ${String(level || "info").toUpperCase()}  ${messageText}`);
            if (this.entries.length > 250) this.entries.splice(0, this.entries.length - 250);
            executionLog.textContent = this.entries.join("\n");
            scrollExecutionLogToEnd();
        },
        begin(action) {
            if (action === "cancel" && this.promptId) this.ignoredPromptIds.add(this.promptId);
            this.active = true;
            this.action = action;
            this.promptId = null;
            const label = action === "cancel" ? "Unlocking project" : `${action} queued`;
            this.render(action === "cancel" ? "Unlocking" : "Queued", label, 0, 1);
            this.append("info", label);
        },
        bindPrompt(promptId) {
            if (promptId != null) this.promptId = String(promptId);
        },
        accepts(detail) {
            if (!this.active) return false;
            const promptId = detail?.prompt_id ?? detail?.promptId;
            if (promptId != null && this.ignoredPromptIds.has(String(promptId))) return false;
            if (promptId != null && this.promptId == null) this.promptId = String(promptId);
            return promptId == null || this.promptId == null || String(promptId) === this.promptId;
        },
        fail(text) {
            this.active = false;
            this.render("Failed", text);
            this.append("error", text);
        },
        stopping() {
            this.render("Stopping", "Interrupting the active ComfyUI prompt and releasing the project lock.");
            this.append("warning", "Stop requested.");
        },
    };
    workspace.execution = execution;

    let executionLogExpanded = false;
    try { executionLogExpanded = globalThis.localStorage?.getItem("longcaster.executionLogExpanded") === "true"; } catch (_) {}
    const renderExecutionLogVisibility = () => {
        executionPanel.classList.toggle("collapsed", !executionLogExpanded);
        executionToggle.textContent = executionLogExpanded ? "Hide Execution Log" : "Show Execution Log";
        executionToggle.setAttribute("aria-expanded", String(executionLogExpanded));
        if (executionLogExpanded) scrollExecutionLogToEnd();
    };
    executionToggle.onclick = () => {
        executionLogExpanded = !executionLogExpanded;
        try { globalThis.localStorage?.setItem("longcaster.executionLogExpanded", String(executionLogExpanded)); } catch (_) {}
        renderExecutionLogVisibility();
    };
    renderExecutionLogVisibility();

    const eventDetail = (event) => event?.detail ?? event ?? {};
    const nodeLabel = (nodeId) => {
        const target = nodeId == null ? null : node.graph?.getNodeById?.(nodeId);
        return target?.title || target?.type || target?.comfyClass || (nodeId == null ? "workflow" : `node ${nodeId}`);
    };
    const listen = (name, handler) => api.addEventListener(name, (event) => handler(eventDetail(event)));
    listen("execution_start", (detail) => {
        if (!execution.accepts(detail)) return;
        execution.render("Running", `${execution.action || "LongCaster"} started`, 0, 1);
        execution.append("info", `Execution started${execution.promptId ? ` · prompt ${execution.promptId}` : ""}.`);
    });
    listen("executing", (detail) => {
        if (!execution.accepts(detail) || detail.node == null) return;
        const label = nodeLabel(detail.node);
        execution.render("Running", label);
        execution.append("info", `Running ${label}.`);
    });
    listen("progress", (detail) => {
        if (!execution.accepts(detail)) return;
        const label = nodeLabel(detail.node ?? detail.node_id);
        execution.render("Running", label, detail.value, detail.max);
    });
    listen("execution_cached", (detail) => {
        if (!execution.accepts(detail)) return;
        const count = Array.isArray(detail.nodes) ? detail.nodes.length : 0;
        if (count) execution.append("info", `${count} node${count === 1 ? "" : "s"} loaded from cache.`);
    });
    listen("executed", (detail) => {
        if (!execution.accepts(detail)) return;
        execution.append("info", `Completed ${nodeLabel(detail.node)}.`);
    });
    listen("execution_success", (detail) => {
        if (!execution.accepts(detail)) return;
        const completedAction = execution.action;
        execution.active = false;
        execution.render("Completed", `${execution.action || "LongCaster"} completed`, 1, 1);
        execution.append("info", "Execution completed.");
        workspace.advanceBatch?.(completedAction);
        workspace.advanceDraftBatch?.(completedAction);
    });
    listen("execution_interrupted", (detail) => {
        if (!execution.accepts(detail)) return;
        execution.active = false;
        const label = `Interrupted at ${nodeLabel(detail.node ?? detail.node_id)}`;
        execution.render("Interrupted", label);
        execution.append("warning", label);
        workspace.cancelBatch?.("Batch rendering stopped after interruption.", true);
        workspace.cancelDraftBatch?.("Draft queue stopped after interruption.", true);
    });
    listen("execution_error", (detail) => {
        if (!execution.accepts(detail)) return;
        execution.active = false;
        const label = nodeLabel(detail.node ?? detail.node_id);
        const error = detail.exception_message || detail.error || "Unknown execution error";
        execution.render("Failed", `${label}: ${error}`);
        execution.append("error", `${label}: ${error}`);
        const traceback = Array.isArray(detail.traceback) ? detail.traceback.join("") : detail.traceback;
        if (traceback) execution.append("error", traceback);
        workspace.cancelBatch?.("Batch rendering stopped after an execution error.", true);
        workspace.cancelDraftBatch?.("Draft queue stopped after an execution error.", true);
    });
    listen("longcaster_log", (detail) => {
        if (execution.active) execution.append(detail.level || "info", detail.message, detail.timestamp);
    });
    stop.onclick = () => {
        workspace.cancelBatch?.("Batch rendering cancelled.", true);
        workspace.cancelDraftBatch?.("Draft queue cancelled.", true);
        stopAndUnlock(node);
    };

    const shortId = (value) => value ? String(value).slice(0, 8) : "none";
    const cardLabel = (cardId) => {
        const item = workspace.state?.cards?.find((card) => card.id === cardId);
        return item ? `Card ${item.timeline_index + 1} · ${item.title || "Untitled"} · ${shortId(item.id)}` : "none";
    };
    const cloneTemplates = (items) => (items || []).map((item) => ({name: item.name || "", text: item.text || ""}));
    const reservedTemplateName = (name) => /^(?:subject|picture|video|audio)\s+\d+$/i.test(name.trim())
        || CARD_SECTIONS.some(([sectionName]) => sectionName.toLowerCase() === name.trim().replace(/^\//, "").toLowerCase());
    const resolveTemplatePreview = (prompt, templates) => {
        const definitions = new Map(templates.map((item) => [item.name.trim(), item.text]));
        const unknown = [];
        const seen = new Set();
        const resolved = String(prompt || "").replace(/<([^<>\r\n]{1,80})>/g, (token, name) => {
            if (definitions.has(name)) return definitions.get(name);
            if (!reservedTemplateName(name) && !seen.has(name)) { unknown.push(name); seen.add(name); }
            return token;
        });
        return {resolved, unknown};
    };
    const markTemplatesDirty = () => {
        workspace.templatesDirty = JSON.stringify(workspace.templateDraft)
            !== JSON.stringify(cloneTemplates(workspace.state?.prompt_templates));
        renderSaveState();
        renderTemplatePreview();
    };
    function renderTemplatePreview() {
        const card = selectedCard();
        const {resolved, unknown} = resolveTemplatePreview(card?.assembled_prompt || "", workspace.templateDraft);
        templatePreview.textContent = resolved || "The selected card prompt is empty.";
        templateWarnings.textContent = unknown.length
            ? `Unresolved template tokens: ${unknown.map((name) => `<${name}>`).join(", ")}`
            : "";
    }
    function renderProjectTemplates() {
        templateList.replaceChildren();
        if (!workspace.templateDraft.length) {
            templateList.append(cardsElement("div", "lc-cards-help", "No project templates defined."));
        }
        workspace.templateDraft.forEach((item, index) => {
            const row = cardsElement("div", "lc-template-row");
            const name = document.createElement("input");
            name.value = item.name; name.maxLength = 80; name.placeholder = "lighting";
            name.setAttribute("aria-label", `Template ${index + 1} name`);
            const text = document.createElement("textarea");
            text.value = item.text; text.maxLength = 20000; text.placeholder = "Reusable prompt text";
            text.setAttribute("aria-label", `Template ${index + 1} replacement text`);
            const remove = cardsElement("button", "lc-identity-delete", "Remove");
            for (const control of [name, text, remove]) control.disabled = Boolean(workspace.state?.pending_operation);
            name.oninput = () => { item.name = name.value; markTemplatesDirty(); };
            text.oninput = () => { item.text = text.value; markTemplatesDirty(); };
            remove.onclick = () => { workspace.templateDraft.splice(index, 1); markTemplatesDirty(); renderProjectTemplates(); };
            row.append(name, text, remove);
            templateList.append(row);
        });
        addTemplate.disabled = Boolean(workspace.state?.pending_operation) || workspace.templateDraft.length >= 64;
        saveTemplates.disabled = Boolean(workspace.state?.pending_operation) || !workspace.templatesDirty;
        renderTemplatePreview();
    }
    const renderSaveState = () => {
        const dirty = workspace.dirty || workspace.loraDirty || workspace.templatesDirty;
        saveState.textContent = workspace.saving ? "Saving…" : dirty ? "Unsaved changes" : `Saved · revision ${workspace.state?.revision ?? "?"}`;
        saveState.className = `lc-cards-save${dirty ? " lc-cards-dirty" : ""}`;
    };
    const renderPromptDiff = (card, take) => {
        promptDiffDetails.hidden = !card || !take;
        if (!card || !take) {
            promptDiff.replaceChildren();
            return;
        }
        const archived = take.assembled_prompt || "";
        const working = card.assembled_prompt || "";
        const entries = archived === working ? [] : promptLineDiff(archived, working);
        const additions = entries.filter((entry) => entry.kind === "add").length;
        const deletions = entries.filter((entry) => entry.kind === "delete").length;
        promptDiffSummary.textContent = entries.length
            ? `Prompt Diff · Take ${take.attempt} (archived) → Working · +${additions} −${deletions}`
            : `Prompt Diff · Take ${take.attempt} matches Working`;
        promptDiff.replaceChildren();
        if (!entries.length) {
            promptDiff.append(cardsElement("div", "lc-prompt-diff-empty", "No prompt differences."));
            return;
        }
        for (const entry of entries) {
            const row = cardsElement("div", `lc-prompt-diff-row ${entry.kind}`);
            row.append(
                cardsElement("span", "line-number", entry.archivedLine ?? ""),
                cardsElement("span", "line-number", entry.workingLine ?? ""),
                cardsElement("span", "", entry.kind === "add" ? "+" : entry.kind === "delete" ? "−" : " "),
                cardsElement("span", "", entry.line),
            );
            promptDiff.append(row);
        }
    };
    const renderGenerationSetup = (take) => {
        generationSetupDetails.hidden = !take;
        generationSetup.replaceChildren();
        if (!take) return;
        generationSetupSummary.textContent = `Take Settings / Generation Setup · Take ${take.attempt}`;
        const setup = take.generation_setup;
        const settingText = (settings) => Object.entries(settings || {})
            .map(([name, value]) => `${name}=${typeof value === "string" ? value : JSON.stringify(value)}`)
            .join(" · ");
        const appendGroup = (label, items, emptyText) => {
            const group = cardsElement("section", "lc-generation-group");
            group.append(cardsElement("strong", "", label));
            if (!items?.length) {
                group.append(cardsElement("div", "lc-cards-help", emptyText));
            } else {
                for (const item of items) {
                    const row = cardsElement("div", "lc-generation-item");
                    const name = item.name || item.title || item.class_type || "Unknown node";
                    row.append(cardsElement("span", "", name));
                    const type = [item.class_type, item.title && item.title !== name ? item.title : null, `node ${item.node_id}`]
                        .filter(Boolean).join(" · ");
                    row.append(cardsElement("small", "", type));
                    const settings = settingText(item.settings);
                    if (settings) row.append(cardsElement("small", "", settings));
                    group.append(row);
                }
            }
            generationSetup.append(group);
        };
        const core = cardsElement("section", "lc-generation-group");
        core.append(cardsElement("strong", "", "Frozen take settings"));
        for (const [name, value] of Object.entries(take.settings || {})) {
            if (value === undefined || value === null || value === "") continue;
            const row = cardsElement("div", "lc-generation-item");
            row.append(
                cardsElement("span", "", name.replaceAll("_", " ")),
                cardsElement("small", "", typeof value === "string" ? value : JSON.stringify(value)),
            );
            core.append(row);
        }
        generationSetup.append(core);
        if (!setup) {
            generationSetup.append(cardsElement("div", "lc-cards-help", "Upstream model provenance was not recorded for this older draft take."));
            return;
        }
        appendGroup("Diffusion models", setup.models, "No recognized diffusion/checkpoint loader on the MODEL path.");
        appendGroup("LoRAs", setup.loras, "No LoRA loader on the MODEL path.");
        appendGroup("Patches / other MODEL-path nodes", setup.patches, "No additional MODEL-path nodes.");
        const bytes = Number(setup.stored_bytes || 0);
        const size = bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${bytes} bytes`;
        generationSetup.append(cardsElement(
            "div", "lc-generation-stats",
            `${setup.node_count} upstream node${setup.node_count === 1 ? "" : "s"} stored · ${size} · graph ${shortId(setup.graph_sha256)}…`,
        ));
        if (setup.truncated) generationSetup.append(cardsElement("div", "lc-generation-warning", "Some unusually large or sensitive values were truncated or redacted."));
    };

    function updateState(state, keepSelection = true) {
        const previousCard = selectedCard();
        const projectChanged = Boolean(workspace.state?.project && workspace.state.project !== state.project);
        const followedSelectedTake = !workspace.viewedTakeId
            || workspace.viewedTakeId === previousCard?.selected_draft_take_id;
        if (workspace.state?.project && workspace.state.project !== state.project) {
            workspace.loraDirty = false;
            workspace.templatesDirty = false;
        }
        if (!workspace.templatesDirty || projectChanged) {
            workspace.templateDraft = cloneTemplates(state.prompt_templates);
        }
        workspace.state = state;
        if (projectChanged) resetProjectPreview();
        workspace.requestedProject = state.project;
        syncProjectNode(node, state);
        projectPath.textContent = state.project_folder || "";
        const exists = state.cards?.some((card) => card.id === workspace.selectedCardId);
        if (!keepSelection || !exists) workspace.selectedCardId = state.active_card_id || state.cards?.[0]?.id;
        const currentCard = selectedCard();
        if (followedSelectedTake || !currentCard?.draft_takes?.some((take) => take.id === workspace.viewedTakeId)) {
            workspace.viewedTakeId = currentCard?.selected_draft_take_id || null;
        }
        render();
    }

    function render() {
        const state = workspace.state;
        if (!state) return;
        const card = selectedCard();
        const batchActive = Boolean(workspace.batch?.active || workspace.draftBatch?.active);
        const showingCard = workspace.activeView === "card";
        main.hidden = !showingCard;
        projectView.hidden = showingCard;
        actions.hidden = !showingCard;
        cardViewTab.classList.toggle("active", showingCard);
        projectViewTab.classList.toggle("active", !showingCard);
        if (!workspace.loraDirty && document.activeElement !== loraWords) {
            loraWords.value = state.lora_activation_words || "";
        }
        saveLoraWords.disabled = Boolean(state.pending_operation)
            || loraWords.value.trim() === (state.lora_activation_words || "");
        renderProjectTemplates();
        duplicateProject.disabled = batchActive || Boolean(state.pending_operation);
        duplicateAndInvalidate.disabled = batchActive || Boolean(state.pending_operation);
        exportProjectPrompts.disabled = batchActive || Boolean(state.pending_operation);
        const projectPreviewCount = selectedProjectPreviewClips().length;
        playProjectPreview.disabled = batchActive || Boolean(state.pending_operation) || !projectPreviewCount;
        playProjectPreview.title = projectPreviewCount
            ? `Play ${projectPreviewCount} selected preview clip${projectPreviewCount === 1 ? "" : "s"}.`
            : "No selected card previews are available.";
        if (document.activeElement !== projectAspect && document.activeElement !== projectMegapixels) {
            projectAspect.value = nearestProjectAspect(state.width, state.height);
            projectMegapixels.value = String(Math.round(state.width * state.height / (1024 * 1024) * 1000) / 1000);
        }
        projectAspect.disabled = projectMegapixels.disabled = batchActive || !state.resolution_editable || Boolean(state.pending_operation);
        const allInvalidated = Boolean(state.cards?.length) && state.cards.every((item) => item.status === "INVALIDATED");
        invalidateAllRenders.disabled = batchActive || Boolean(state.pending_operation) || allInvalidated;
        resolutionLock.textContent = state.resolution_editable
            ? (allInvalidated ? "All cards are invalidated. Resolution can now be changed." : "Resolution can be changed before the first render.")
            : "To change resolution, invalidate all cards first.";
        resolutionLock.className = state.resolution_editable ? "lc-cards-help" : "lc-project-resolution-lock";
        renderProjectResolutionChoice();
        list.replaceChildren();
        for (const item of state.cards || []) {
            const row = cardsElement("div", "lc-card-row");
            const button = cardsElement("button", `lc-card-item${item.id === card?.id ? " selected" : ""}`);
            const number = cardsElement("div", "lc-card-number", String(item.timeline_index + 1).padStart(2, "0"));
            const body = cardsElement("div", "");
            body.append(cardsElement("div", "lc-card-title", item.title || "Untitled card"));
            body.append(cardsElement("div", `lc-card-meta lc-render-${item.render_validity}`, `${item.status} · ${item.render_validity} · ${Number(item.actual_duration_seconds ?? item.requested_duration_seconds ?? 0).toFixed(1)}s`));
            button.append(number, body);
            button.onclick = async () => { if (await flush()) { workspace.selectedCardId = item.id; workspace.viewedTakeId = item.selected_draft_take_id || null; render(); } };
            const copyPrompt = cardsElement("button", "lc-card-copy", "⧉");
            copyPrompt.title = `Copy Card ${item.timeline_index + 1}'s full prompt`;
            copyPrompt.setAttribute("aria-label", copyPrompt.title);
            copyPrompt.onclick = async () => {
                try {
                    await copyTextToClipboard(item.assembled_prompt || "");
                    setMessage(`Card ${item.timeline_index + 1} full prompt copied.`);
                } catch (error) {
                    setMessage(`Could not copy Card ${item.timeline_index + 1}'s prompt: ${error.message}`, true);
                }
            };
            row.append(button, copyPrompt);
            list.append(row);
        }
        if (!card) return;
        const isEditable = editable(card) && !workspace.draftBatch?.active;
        let take = viewedTake(card);
        if (!take && card.selected_draft_take_id) {
            workspace.viewedTakeId = card.selected_draft_take_id;
            take = viewedTake(card);
        }
        cardTitle.textContent = `Card ${card.timeline_index + 1}`;
        cardStatus.textContent = `${card.status} · ${card.render_validity} · ${card.id}`;
        cardStatus.className = `lc-render-${card.render_validity}`;
        previousCard.disabled = batchActive || card.timeline_index < 1;
        nextCard.disabled = batchActive || card.timeline_index >= state.cards.length - 1;
        tabs.replaceChildren();
        for (const [name, label] of CARD_SECTIONS) {
            const button = cardsElement("button", `lc-cards-tab${workspace.selectedSection === name ? " active" : ""}`, label);
            button.onclick = () => { workspace.selectedSection = name; render(); textarea.focus(); scrollFullToSection(); };
            tabs.append(button);
        }
        const structured = card.prompt_format === "structured_v1";
        const importing = workspace.importCardId === card.id;
        importPanel.hidden = !importing;
        fullPrompt.hidden = importing;
        sectionColumn.hidden = !structured;
        legacy.hidden = structured;
        fullPrompt.textContent = card.assembled_prompt || "";
        if (structured) {
            const record = card.prompt_sections[workspace.selectedSection];
            sectionName.textContent = CARD_SECTIONS.find(([name]) => name === workspace.selectedSection)?.[1] || workspace.selectedSection;
            textarea.value = record.text;
            textarea.readOnly = !isEditable;
            const source = record.provenance.source_card_id ? ` · source ${record.provenance.source_card_id}` : "";
            provenance.textContent = `${record.provenance.source_type}${source}${record.provenance.modified_after_copy ? " · modified after copy" : ""}`;
        } else {
            legacyPrompt.textContent = card.assembled_prompt;
            legacyMessage.textContent = "Historical flat prompt. Recognized canonical headings will be assigned to their matching sections; otherwise the full text will be placed in Detailed Description.";
        }
        sourceSelect.replaceChildren();
        for (const source of state.cards.filter((item) => item.id !== card.id && item.prompt_format === "structured_v1")) {
            const option = document.createElement("option"); option.value = source.id; option.textContent = `Card ${source.timeline_index + 1}`; sourceSelect.append(option);
        }
        if (card.timeline_predecessor_id) sourceSelect.value = card.timeline_predecessor_id;
        copyPrevious.disabled = !isEditable || !structured || !card.timeline_predecessor_id;
        copySelected.disabled = !isEditable || !structured || !sourceSelect.value;
        clearSection.disabled = !isEditable || !structured;
        convertLegacy.disabled = !isEditable || structured;
        importFull.disabled = !isEditable;
        duration.value = card.requested_duration_seconds ?? 5;
        seed.value = String(card.seed ?? 0);
        duration.disabled = !isEditable; seed.disabled = !isEditable;
        refImageSize.value = card.ref_image_size || "match";
        renderCardReference();
        renderPictureRail(card, take);
        renderCardLoras(card, isEditable);
        refImageSize.disabled = !isEditable || state.generation_mode !== "ref2va";
        refImageSize.title = state.generation_mode === "ref2va"
            ? "Choose how REF2VA reference images are sized for this card."
            : "Reference image sizing is used only by REF2VA projects.";
        mode.value = state.generation_mode; mode.disabled = !state.generation_mode_editable;
        mode.title = state.generation_mode_editable ? "Mode can change until the first render." : "Mode is fixed because this project already has rendered media.";
        treatment.value = state.continuation_treatment || "off";
        treatment.disabled = Boolean(state.pending_operation) || batchActive;
        colorBaseline.replaceChildren();
        for (const candidate of state.cards.filter((item) => item.status === "ACCEPTED" && item.timeline_index < card.timeline_index)) {
            const option = document.createElement("option");
            option.value = candidate.id;
            option.textContent = `Card ${candidate.timeline_index + 1}${candidate.title ? ` · ${candidate.title}` : ""}`;
            colorBaseline.append(option);
        }
        colorBaseline.value = card.color_anchor_source_card_id || "";
        colorBaseline.disabled = !isEditable || card.continuation_strategy !== "direct_mmh3" || state.continuation_treatment !== "color_stable" || !colorBaseline.options.length;
        colorBaseline.title = state.continuation_treatment !== "color_stable"
            ? "Enable Drift + Color-Stable AV to use this baseline. The saved choice is retained while treatment is off."
            : card.continuation_strategy !== "direct_mmh3"
                ? "Color-Stable baselines apply only to direct MMH3 continuation."
                : "Select the accepted card whose color statistics should anchor this scene.";
        identityOverrides.replaceChildren();
        const boundSubjects = Object.keys(state.active_identity_anchors || {}).sort((left, right) => left.localeCompare(right));
        if (!boundSubjects.length) {
            identityOverrides.append(cardsElement("div", "lc-cards-help", "No project identity checkpoint is selected for any subject."));
        }
        for (const subject of boundSubjects) {
            const row = cardsElement("div", "lc-card-identity-override");
            const label = cardsElement("span", "", subject);
            const select = document.createElement("select");
            const projectAnchorId = state.active_identity_anchors[subject];
            const projectAnchor = state.identity_anchors?.find(item => item.anchor_id === projectAnchorId);
            const projectLabel = `Project (${projectAnchor?.enabled === false ? "Disabled" : "Enabled"})`;
            for (const [value, text] of [["project", projectLabel], ["enabled", "Enabled"], ["disabled", "Disabled"]]) {
                const option = document.createElement("option"); option.value = value; option.textContent = text; select.append(option);
            }
            select.value = card.identity_anchor_overrides?.[subject] || "project";
            select.disabled = !isEditable || Boolean(state.pending_operation) || batchActive;
            select.onchange = () => {
                card.identity_anchor_overrides = {...(card.identity_anchor_overrides || {}), [subject]: select.value};
                markDirty();
                renderPictureRail(card, viewedTake());
            };
            row.append(label, select); identityOverrides.append(row);
        }
        const refineMode = state.refine_cadence || "off";
        setRefineRadios(refineCadence, refineMode);
        setRefineRadios(subjectRefineCadence, refineMode);
        disableRefineRadios(refineCadence, Boolean(state.pending_operation) || batchActive);
        disableRefineRadios(subjectRefineCadence, Boolean(state.pending_operation) || batchActive);
        refineEnabled.checked = Boolean(card.refine_enabled);
        subjectRefineEnabled.checked = Boolean(card.refine_enabled);
        const refineCheckboxDisabled = !isEditable || refineMode !== "manual" || Boolean(state.pending_operation);
        refineEnabled.disabled = refineCheckboxDisabled;
        subjectRefineEnabled.disabled = refineCheckboxDisabled;
        refineEnabled.title = subjectRefineEnabled.title = refineMode === "off"
            ? "Continuation refine is Off and overrides this card setting."
            : refineMode === "every_accepted_card"
                ? "Auto refines every accepted card and overrides this card setting."
                : isEditable ? "Refine this card immediately after acceptance." : "Accepted cards are read-only.";
        subjectRefine.hidden = workspace.selectedSection !== "subject_definitions";
        strategy.value = card.continuation_strategy || "independent";
        strategy.disabled = !isEditable || !card.timeline_predecessor_id;
        generate.disabled = batchActive || card.id !== state.active_card_id || !["EMPTY", "FAILED", "INVALIDATED"].includes(card.status) || Boolean(state.pending_operation);
        queueDrafts.disabled = generate.disabled;
        draftCount.disabled = batchActive;
        retry.disabled = batchActive || card.id !== state.active_card_id || card.status !== "DRAFT" || Boolean(state.pending_operation);
        retryDifferentSeed.disabled = retry.disabled;
        retryDifferentSeed.title = "Assign a different seed for each retry; use Draft count for multiple takes and keep Studio open.";
        accept.disabled = batchActive || card.id !== state.active_card_id || card.status !== "DRAFT" || card.draft_inputs_dirty || Boolean(state.pending_operation) || Boolean(take && !take.selected);
        accept.title = take && !take.selected ? "Select the take you are viewing before accepting it." : "Accept the selected draft take.";
        const batchPlan = remainingBatchPlan(state);
        batchRender.textContent = workspace.batch?.active
            ? "Cancel Batch After Current Step"
            : `Render Remaining Drafts${batchPlan ? ` (${batchPlan.renderCount})` : ""}`;
        batchRender.disabled = Boolean(workspace.draftBatch?.active) || (!workspace.batch?.active && (!batchPlan || Boolean(state.pending_operation)));
        batchRender.title = workspace.batch?.active
            ? "Stop automatically queueing cards after the current Generate or Accept operation finishes."
            : batchPlan
                ? "Generate the remaining invalidated cards in order, automatically accepting intermediates and leaving the final card as a draft."
                : "Batch rendering requires an active draft followed only by invalidated cards.";
        append.disabled = batchActive || card.id !== state.active_card_id || card.status !== "ACCEPTED" || Boolean(state.pending_operation);
        const isLastCard = card.timeline_index === state.cards.length - 1;
        const isRemovableDraft = ["EMPTY", "DRAFT", "FAILED", "INVALIDATED"].includes(card.status);
        invalidateRender.hidden = card.render_validity !== "VALIDATED";
        invalidateRender.disabled = batchActive || Boolean(state.pending_operation);
        invalidateRender.title = "Permanently delete this card's render and every later render while retaining their prompts and editable settings.";
        unpublish.hidden = card.status !== "ACCEPTED";
        unpublish.disabled = batchActive || !isLastCard || card.id !== state.active_card_id || Boolean(state.pending_operation);
        unpublish.title = !isLastCard
            ? "Remove the later unaccepted draft first, then this accepted card can be unpublished to a draft."
            : card.id !== state.active_card_id
                ? "Only the active accepted card can be unpublished to a draft."
                : state.pending_operation
                    ? "Wait for the current project operation to finish before unpublishing."
                    : "Reopen this accepted card as a draft, retain its immutable master, and permanently remove its refine derivatives.";
        removeDraft.hidden = !isLastCard || !isRemovableDraft;
        removeDraft.disabled = batchActive || card.id !== state.active_card_id || state.cards.length < 2 || Boolean(state.pending_operation);
        removeDraft.title = state.cards.length < 2
            ? "The first card cannot be removed because there is no previous accepted card."
            : card.has_publication_history
                ? "Permanently discard this draft and all of its prior publication history, then return to the previous accepted card."
                : "Discard this unaccepted card and return to the previous accepted card.";
        const viewedPreviewAvailable = take ? take.preview_available : card.preview_available;
        if (viewedPreviewAvailable) {
            const previewQuery = new URLSearchParams({
                project: state.project,
                card: card.id,
                version: take?.preview_version || card.preview_version || card.updated_at || "",
            });
            if (take) previewQuery.set("take", take.id);
            const nextSource = api.apiURL(`/longcaster/cards/preview?${previewQuery.toString()}`);
            if (video.dataset.source !== nextSource) { video.dataset.source = nextSource; video.src = nextSource; video.load(); }
        } else if (video.dataset.source) {
            video.pause(); video.removeAttribute("src"); video.dataset.source = ""; video.load();
        }
        buildPreview.hidden = viewedPreviewAvailable || card.status !== "ACCEPTED" || (take && !take.selected);
        draftTakes.replaceChildren();
        const takes = [...(card.draft_takes || [])].reverse();
        if (!takes.length) draftTakes.append(cardsElement("div", "lc-cards-help", "No completed draft takes yet."));
        for (const item of takes) {
            const row = cardsElement("div", `lc-draft-take${item.selected ? " selected" : ""}${item.id === workspace.viewedTakeId ? " viewed" : ""}`);
            const body = cardsElement("div", "lc-draft-take-meta");
            const created = item.created_at ? new Date(item.created_at).toLocaleString() : "unknown time";
            body.append(
                cardsElement("strong", "", `Take ${item.attempt}${item.selected ? " · selected" : ""}`),
                cardsElement("small", "", `Seed ${item.seed} · ${Number(item.actual_duration_seconds ?? item.requested_duration_seconds ?? 0).toFixed(2)}s · ${created}`),
                ...(item.prompt_excerpt ? [cardsElement("small", "", item.prompt_excerpt)] : []),
                cardsElement("small", "", item.preview_available ? "Preview saved" : "No saved preview"),
            );
            const controls = cardsElement("div", "lc-draft-take-actions");
            const viewTake = cardsElement("button", "", item.id === workspace.viewedTakeId ? "Viewing" : "View");
            viewTake.disabled = item.id === workspace.viewedTakeId;
            viewTake.onclick = () => { workspace.viewedTakeId = item.id; render(); };
            const selectTake = cardsElement("button", item.selected ? "" : "lc-cards-accept", item.selected ? "Selected" : "Select");
            selectTake.disabled = item.selected || card.status !== "DRAFT" || card.id !== state.active_card_id || Boolean(state.pending_operation);
            selectTake.title = card.status !== "DRAFT" ? "Unpublish this card before selecting another take." : "Restore this take's prompt, seed, and exact generation settings, then use it for acceptance.";
            selectTake.onclick = () => manageDraftTake("select", card, item);
            const deleteTake = cardsElement("button", "lc-identity-delete", "Delete");
            deleteTake.disabled = item.selected || Boolean(state.pending_operation);
            deleteTake.onclick = () => {
                if (confirm(`Permanently delete Take ${item.attempt} and its saved preview?`)) manageDraftTake("delete", card, item);
            };
            controls.append(viewTake, selectTake, deleteTake);
            row.append(body, controls);
            draftTakes.append(row);
        }
        renderPromptDiff(card, take);
        renderGenerationSetup(take);
        deleteUnselectedTakes.hidden = takes.length < 2;
        deleteUnselectedTakes.disabled = Boolean(state.pending_operation);
        const currentAnchor = card.current_state_anchor;
        info.replaceChildren(
            cardsElement("div", "", `Timeline predecessor: ${cardLabel(card.timeline_predecessor_id)}`),
            cardsElement("div", "", `Generation parent: ${cardLabel(card.generation_parent_id)}`),
            cardsElement("div", "", `Accepted take: ${card.accepted_publication_id ? `publication ${shortId(card.accepted_publication_id)}` : "none"}`),
            cardsElement("div", "", currentAnchor ? `Current state: Card ${card.timeline_index + 1} final frame ${currentAnchor.source_frame_index} at ${Number(currentAnchor.source_timestamp_seconds).toFixed(2)}s` : "Current state: no anchor"),
            cardsElement("div", "", `Prompt fingerprint: ${shortId(card.prompt_hash)}…`),
            ...(card.draft_inputs_dirty ? [cardsElement("div", "lc-cards-dirty", "Draft inputs changed · Retry Draft before accepting")]: []),
            ...(card.last_error ? [cardsElement("div", "lc-cards-error", `Last error: ${card.last_error}`)] : []),
        );
        const derivative = card.continuation_refine;
        const derivativeState = derivative?.status || "NOT_CREATED";
        refineStatus.replaceChildren(
            cardsElement("div", `lc-status-${derivativeState === "READY" ? "ACCEPTED" : derivativeState === "FAILED" ? "FAILED" : "EMPTY"}`, `Status: ${derivativeState.replaceAll("_", " ")}`),
            cardsElement("div", "", `Default source: ${card.continuation_source_preference === "derivative" ? "Refined derivative" : "Accepted master"}`),
            ...(derivative?.last_error ? [cardsElement("div", "lc-cards-error", derivative.last_error)] : []),
        );
        createRefine.hidden = card.status !== "ACCEPTED";
        createRefine.textContent = derivativeState === "READY" ? "Regenerate Refine" : "Generate Refine";
        createRefine.disabled = card.id !== state.active_card_id || refineMode === "off" || Boolean(state.pending_operation);
        createRefine.title = refineMode === "off" ? "Continuation refine is Off." : "Generate a new refine derivative for this accepted card.";
        useAcceptedMaster.hidden = card.status !== "ACCEPTED";
        useAcceptedMaster.disabled = card.continuation_source_preference === "accepted_master" || Boolean(state.pending_operation);
        useRefined.hidden = card.status !== "ACCEPTED";
        useRefined.disabled = derivativeState !== "READY" || card.continuation_source_preference === "derivative" || Boolean(state.pending_operation);
        renderIdentities();
        renderIdentitySource();
        renderReferences(card);
        renderSaveState();
    }

    function scrollFullToSection() {
        const card = selectedCard();
        if (!card || card.prompt_format !== "structured_v1") return;
        const start = card.assembled_prompt.indexOf(`${workspace.selectedSection}:`);
        const ratio = start < 0 ? 0 : start / Math.max(1, card.assembled_prompt.length);
        fullPrompt.scrollTop = ratio * Math.max(0, fullPrompt.scrollHeight - fullPrompt.clientHeight);
    }

    function renderIdentities() {
        identities.replaceChildren();
        const items = workspace.state.identity_anchors || [];
        if (!items.length) identities.append(cardsElement("div", "lc-cards-help", "No saved identity checkpoints. Choose an accepted card frame or import an image below."));
        for (const item of items) {
            const selected = Boolean(item.active_for?.length);
            const inUse = selected && item.enabled;
            const row = cardsElement("div", `lc-cards-asset${inUse ? " in-use" : ""}`);
            const image = document.createElement("img");
            if (item.asset_available) image.src = api.apiURL(`/longcaster/cards/anchor?project=${encodeURIComponent(workspace.state.project)}&anchor=${encodeURIComponent(item.anchor_id)}`);
            const body = document.createElement("div");
            const binding = inUse
                ? `IN USE FOR ${item.active_for.join(", ")}`
                : selected ? `SELECTED FOR ${item.active_for.join(", ")} (DISABLED)` : "SAVED - NOT IN USE";
            body.append(cardsElement("div", "", item.label));
            body.append(cardsElement("small", "", `${binding} · ${item.enabled ? "ENABLED" : "DISABLED"}`));
            const sourceTime = item.source_timestamp_seconds == null ? "" : ` at ${Number(item.source_timestamp_seconds).toFixed(2)}s`;
            const sourceDescription = item.selection_source === "imported_image"
                ? "Imported image"
                : `Card ${item.source_card_number}, frame ${item.source_preview_frame_index ?? "?"}${sourceTime}`;
            body.append(cardsElement("small", "", `${item.subject_id} · ${sourceDescription} · ${item.identity_scope.replaceAll("_", " ")} · ${shortId(item.anchor_id)}`));
            if (item.custom_identity_instruction) body.append(cardsElement("small", "", item.custom_identity_instruction));
            const controls = cardsElement("div", "lc-identity-controls");
            const use = cardsElement("button", "", selected ? (item.enabled ? "Disable" : "Enable") : "Use");
            use.disabled = Boolean(workspace.state.pending_operation);
            use.onclick = () => manageIdentity(selected ? (item.enabled ? "disable" : "enable") : "bind", item);
            controls.append(use);
            if (selected) {
                const clear = cardsElement("button", "", "Clear");
                clear.disabled = Boolean(workspace.state.pending_operation);
                clear.onclick = () => manageIdentity("clear", item);
                controls.append(clear);
            }
            const exportImage = cardsElement("button", "", "Export PNG");
            exportImage.disabled = !item.asset_available;
            exportImage.onclick = () => {
                const link = document.createElement("a");
                link.href = api.apiURL(`/longcaster/cards/anchor?project=${encodeURIComponent(workspace.state.project)}&anchor=${encodeURIComponent(item.anchor_id)}&download=1`);
                link.download = "";
                document.body.append(link); link.click(); link.remove();
            };
            controls.append(exportImage);
            const remove = cardsElement("button", "lc-identity-delete", "Delete");
            remove.disabled = Boolean(workspace.state.pending_operation);
            remove.onclick = () => {
                const warning = selected
                    ? `Delete this identity checkpoint? It is selected for ${item.active_for.join(", ")} and will be unbound before its saved image is permanently removed.`
                    : "Delete this saved identity checkpoint and permanently remove its image?";
                if (confirm(warning)) manageIdentity("delete", item);
            };
            controls.append(remove);
            row.append(image, body, controls); identities.append(row);
        }
    }

    function renderIdentitySource() {
        const accepted = workspace.state.cards.filter((card) => card.status === "ACCEPTED");
        if (!identitySourceCard() && accepted.length) workspace.identitySourceCardId = accepted[accepted.length - 1].id;
        const source = identitySourceCard();
        identitySourceSelect.replaceChildren();
        for (const card of accepted) {
            const option = document.createElement("option");
            option.value = card.id;
            option.textContent = `Card ${card.timeline_index + 1} · ${card.title || "Untitled"}`;
            identitySourceSelect.append(option);
        }
        identitySourceSelect.value = source?.id || "";
        identitySourceSelect.disabled = !source;
        identityForm.hidden = false;
        identityFrameField.hidden = !source;
        createIdentity.hidden = !source;
        buildIdentityPreview.hidden = !source || source.preview_available;
        buildIdentityPreview.disabled = Boolean(workspace.state.pending_operation);
        const importDisabled = Boolean(workspace.state.pending_operation) || workspace.state.generation_mode !== "ref2va";
        identityImport.classList.toggle("disabled", importDisabled);
        identityImportInput.disabled = importDisabled;
        const visibleFrames = Number(source?.actual_new_frame_count ?? Math.round(Number(source?.actual_duration_seconds || 0) * 24));
        identityFrame.max = String(Math.max(0, visibleFrames - 1));
        if (source?.preview_available) {
            const query = new URLSearchParams({
                project: workspace.state.project,
                card: source.id,
                version: source.preview_version || source.updated_at || "",
            });
            const nextSource = api.apiURL(`/longcaster/cards/preview?${query.toString()}`);
            if (identityVideo.dataset.source !== nextSource) {
                identityVideo.dataset.source = nextSource;
                identityVideo.src = nextSource;
                identityVideo.load();
            }
            createIdentity.disabled = Boolean(workspace.state.pending_operation);
        } else {
            if (identityVideo.dataset.source) {
                identityVideo.pause(); identityVideo.removeAttribute("src"); identityVideo.dataset.source = ""; identityVideo.load();
            }
            createIdentity.disabled = true;
        }
    }

    function renderReferences(card) {
        references.replaceChildren();
        const live = connectedReferenceResources();
        const source = card.reference_set?.resources?.length
            ? card.reference_set
            : [...workspace.state.cards].reverse().find((item) => item.reference_set?.resources?.length)?.reference_set;
        const counters = {image: 0, video: 0, audio: 0};
        if (live.resources.length) {
            references.append(cardsElement("div", "lc-cards-help", `Connected now: ${live.packetName}. Changes here are recorded into the card when it generates.`));
            for (const item of live.resources) {
                counters[item.kind] = (counters[item.kind] || 0) + 1;
                const token = item.kind === "image" ? "Picture" : item.kind === "video" ? "Video" : item.kind === "audio" ? "Audio" : "Resource";
                const row = cardsElement("div", "lc-cards-asset");
                let icon;
                if (item.kind === "image" && item.filename) {
                    icon = document.createElement("img");
                    const query = new URLSearchParams({filename: item.filename, type: "input"});
                    icon.src = api.apiURL(`/view?${query.toString()}`);
                    icon.alt = item.name || item.filename;
                } else icon = cardsElement("div", "", item.kind === "image" ? "IMG" : item.kind === "video" ? "VID" : "AUD");
                const body = document.createElement("div");
                body.append(cardsElement("div", "", `<${token} ${counters[item.kind]}> · ${item.name || "unnamed resource"}`));
                body.append(cardsElement("small", "", `${item.kind} · role ${item.role || "reference"}${item.filename ? ` · ${item.filename}` : ""}`));
                row.append(icon, body); references.append(row);
            }
            return;
        }
        if (!source?.resources?.length) {
            references.append(cardsElement("div", "lc-cards-help", "No reference snapshot has been recorded yet. Resources arrive from the MMH3 packet connected in the graph."));
            return;
        }
        references.append(cardsElement("div", "lc-cards-help", `Packet: ${source.packet_name || shortId(source.packet_id)}. Media content remains owned by the connected MMH3 reference nodes.`));
        for (const item of source.resources) {
            counters[item.kind] = (counters[item.kind] || 0) + 1;
            const token = item.kind === "image" ? "Picture" : item.kind === "video" ? "Video" : item.kind === "audio" ? "Audio" : "Resource";
            const row = cardsElement("div", "lc-cards-asset");
            const icon = cardsElement("div", "", item.kind === "image" ? "IMG" : item.kind === "video" ? "VID" : "AUD");
            const body = document.createElement("div");
            body.append(cardsElement("div", "", `<${token} ${counters[item.kind]}>`));
            body.append(cardsElement("small", "", `${item.kind} · resource ${shortId(item.id)}`));
            row.append(icon, body); references.append(row);
        }
    }

    function linkedSource(target, inputName) {
        const input = target?.inputs?.find((item) => item.name === inputName);
        const link = input?.link == null ? null : target.graph?.links?.[input.link];
        return link ? target.graph?.getNodeById?.(link.origin_id) : null;
    }

    function connectedReferenceResources() {
        let packet = linkedSource(node, "reference_packet");
        const packetName = String(widget(packet, "name")?.value || packet?.title || packet?.type || "MMH3 reference packet");
        const resources = [];
        const visited = new Set();
        while (packet && !visited.has(packet.id)) {
            visited.add(packet.id);
            const type = String(packet.comfyClass || packet.type || "");
            if (type !== "MMH3Put") break;
            const resourceNode = linkedSource(packet, "resource");
            const resourceType = String(resourceNode?.comfyClass || resourceNode?.type || "").toLowerCase();
            const resourceInput = packet.inputs?.find((item) => item.name === "resource");
            const resourceLink = resourceInput?.link == null ? null : packet.graph?.links?.[resourceInput.link];
            const linkType = String(resourceLink?.type || "").toLowerCase();
            const kind = resourceType.includes("image") || linkType.includes("image") ? "image"
                : resourceType.includes("audio") || linkType.includes("audio") ? "audio" : "video";
            resources.unshift({
                kind,
                role: String(widget(packet, "role")?.value || "reference"),
                name: String(widget(packet, "name")?.value || resourceNode?.title || resourceNode?.type || ""),
                filename: kind === "image" ? String(widget(resourceNode, "image")?.value || "") : "",
            });
            packet = linkedSource(packet, "packet");
        }
        const directResources = (packet?.inputs || []).flatMap((input) => {
            const match = /^(image|video|audio)_(\d+)$/.exec(String(input.name || ""));
            if (!match) return [];
            const resourceNode = linkedSource(packet, input.name);
            if (!resourceNode) return [];
            const kind = match[1];
            return [{
                kind,
                index: Number(match[2]),
                role: "reference",
                name: String(resourceNode.title || resourceNode.type || `${kind} ${Number(match[2]) + 1}`),
                filename: kind === "image" ? String(widget(resourceNode, "image")?.value || "") : "",
            }];
        }).sort((left, right) => {
            const rank = {image: 0, video: 1, audio: 2};
            return rank[left.kind] - rank[right.kind] || left.index - right.index;
        });
        resources.unshift(...directResources);
        return {packetName, resources};
    }

    function renderCardReference() {
        cardReferenceControls.render({
            project: workspace.state.project, card: selectedCard(),
            generationMode: workspace.state.generation_mode,
            editable: editable(selectedCard()) && !workspace.referenceBusy,
            observedReferences: workspace.state.observed_reference_set,
        });
    }

    function renderPictureRail(card, take) {
        const live = connectedReferenceResources();
        const useFrozen = card.status === "ACCEPTED" || Boolean(take && !take.selected);
        const frozenResources = take?.settings?.references?.resources
            || card.reference_set?.resources
            || workspace.state.observed_reference_set?.resources
            || [];
        const globalSource = workspace.state.generation_mode !== "ref2va"
            ? []
            : (useFrozen || !live.resources.length ? frozenResources : live.resources);
        const livePictures = live.resources.filter(item => item.kind === "image");
        const globalPictures = globalSource.filter(item => item.kind === "image").map((item, index) => {
            const liveItem = livePictures[index];
            const thumbnail = liveItem?.filename
                ? api.apiURL(`/view?${new URLSearchParams({filename: liveItem.filename, type: "input"})}`)
                : null;
            const frozenId = shortId(item.id);
            return {
                label: item.name || item.role || `Project Picture ${index + 1}`,
                detail: useFrozen
                    ? thumbnail
                        ? `current graph preview · frozen ${frozenId}`
                        : `frozen resource ${frozenId} · thumbnail unavailable`
                    : thumbnail ? "connected project packet" : `project packet · ${frozenId}`,
                thumbnail,
            };
        });
        const selectedCardReference = useFrozen
            ? (take?.settings?.card_reference || card.card_reference_resolved)
            : card.card_reference;
        const cardReference = selectedCardReference ? {
            enabled: Boolean(selectedCardReference.enabled),
            label: "Card Reference",
            detail: selectedCardReference.enabled
                ? `${selectedCardReference.latent_mode}${selectedCardReference.latent_mode === "soften" ? ` · ${Number(selectedCardReference.latent_strength).toFixed(2)}` : ""}`
                : "Disabled; no prompt number",
            thumbnail: api.apiURL(`/longcaster/cards/reference?${new URLSearchParams({
                project: workspace.state.project, card_id: card.id, reference_id: selectedCardReference.id,
            })}`),
        } : null;
        let resolvedIdentities = [];
        if (workspace.state.generation_mode === "ref2va") {
            if (useFrozen) {
                const frozen = take?.settings?.identity_anchors?.length
                    ? take.settings.identity_anchors
                    : card.identity_anchors_resolved?.length
                        ? card.identity_anchors_resolved
                        : [take?.settings?.identity_anchor || card.identity_anchor_resolved].filter(item => item?.active);
                resolvedIdentities = frozen.map(info => workspace.state.identity_anchors?.find(item => item.anchor_id === info.anchor_id)).filter(Boolean);
            } else if (widget(node, "use_identity_anchor")?.value !== false) {
                for (const [subject, anchorId] of Object.entries(workspace.state.active_identity_anchors || {}).sort(([left], [right]) => left.localeCompare(right))) {
                    const item = workspace.state.identity_anchors?.find(candidate => candidate.anchor_id === anchorId);
                    const policy = card.identity_anchor_overrides?.[subject] || "project";
                    if (item && (policy === "enabled" || (policy === "project" && item.enabled !== false))) resolvedIdentities.push(item);
                }
            }
        }
        const identityReferences = resolvedIdentities.map(activeIdentity => ({
            enabled: true,
            label: activeIdentity.label || activeIdentity.subject_id || "Identity Anchor",
            detail: activeIdentity.selection_source === "imported_image"
                ? `${activeIdentity.subject_id || "identity"} · Imported image`
                : `${activeIdentity.subject_id || "identity"} · Card ${activeIdentity.source_card_number || activeIdentity.source_card_artifact_number || "?"}`,
            thumbnail: api.apiURL(`/longcaster/cards/anchor?project=${encodeURIComponent(workspace.state.project)}&anchor=${encodeURIComponent(activeIdentity.anchor_id)}`),
        }));
        const result = assemblePictureReferences({globalPictures, cardReference, identityReferences});
        pictureToggle.textContent = `Pictures (${result.count})`;
        pictureToggle.classList.toggle("active", workspace.pictureRailOpen);
        pictureToggle.setAttribute("aria-expanded", String(workspace.pictureRailOpen));
        editing.classList.toggle("picture-rail-open", workspace.pictureRailOpen);
        pictureRail.render(result, {
            open: workspace.pictureRailOpen,
            sourceNote: useFrozen
                ? livePictures.some(item => item.filename) ? "frozen take · current graph preview" : "frozen take"
                : live.resources.length ? "connected graph" : "last snapshot",
        });
    }

    async function loadCardLoraCatalog(showMessage = false) {
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/loras"));
            workspace.availableLoras = [...(body.loras || [])];
            workspace.loraCatalogLoaded = true;
            if (workspace.state) renderCardLoras(selectedCard(), editable(selectedCard()));
            if (showMessage) setMessage(`Found ${workspace.availableLoras.length} installed LoRA${workspace.availableLoras.length === 1 ? "" : "s"}.`);
            return true;
        } catch (error) {
            workspace.loraCatalogLoaded = false;
            if (showMessage) setMessage(`LoRA list refresh failed: ${error.message}`, true);
            return false;
        }
    }

    function renderCardLoras(card, isEditable) {
        cardLoraList.replaceChildren();
        const stack = card?.lora_stack || [];
        if (!stack.length) {
            cardLoraList.append(cardsElement("div", "lc-cards-help", "No card LoRAs. The connected graph model remains unchanged."));
        }
        stack.forEach((item, index) => {
            const row = cardsElement("div", "lc-card-lora-row");
            const enabled = document.createElement("input"); enabled.type = "checkbox"; enabled.checked = item.enabled !== false;
            enabled.title = "Enable this LoRA and its activation words";
            const name = document.createElement("select");
            const choices = workspace.availableLoras.includes(item.lora_name)
                ? workspace.availableLoras : [item.lora_name, ...workspace.availableLoras].filter(Boolean);
            if (!choices.length) {
                const option = document.createElement("option"); option.value = ""; option.textContent = "No installed LoRAs found"; name.append(option);
            } else {
                for (const value of choices) {
                    const option = document.createElement("option"); option.value = value; option.textContent = value; name.append(option);
                }
            }
            name.value = item.lora_name || choices[0] || "";
            const strength = document.createElement("input"); strength.type = "number"; strength.min = "-100"; strength.max = "100"; strength.step = "0.05"; strength.value = String(item.strength_model ?? 1);
            strength.title = "Model strength";
            const words = document.createElement("input"); words.type = "text"; words.maxLength = 2000; words.value = item.activation_words || ""; words.placeholder = "Activation words (optional)";
            const actions = cardsElement("div", "lc-card-lora-actions");
            const up = cardsElement("button", "", "↑"); const down = cardsElement("button", "", "↓"); const remove = cardsElement("button", "lc-identity-delete", "Remove");
            for (const control of [enabled, name, strength, words, up, down, remove]) control.disabled = !isEditable;
            up.disabled = !isEditable || index === 0; down.disabled = !isEditable || index === stack.length - 1;
            enabled.onchange = () => { item.enabled = enabled.checked; markDirty(); };
            name.onchange = () => { item.lora_name = name.value; markDirty(); };
            strength.oninput = () => { item.strength_model = Number(strength.value); markDirty(); };
            words.oninput = () => { item.activation_words = words.value; markDirty(); };
            up.onclick = () => { [stack[index - 1], stack[index]] = [stack[index], stack[index - 1]]; markDirty(); renderCardLoras(card, isEditable); };
            down.onclick = () => { [stack[index + 1], stack[index]] = [stack[index], stack[index + 1]]; markDirty(); renderCardLoras(card, isEditable); };
            remove.onclick = () => { stack.splice(index, 1); markDirty(); renderCardLoras(card, isEditable); };
            actions.append(up, down, remove);
            row.append(enabled, name, strength, words, actions);
            cardLoraList.append(row);
        });
        addCardLora.disabled = !isEditable || stack.length >= 8 || !workspace.availableLoras.length;
        addCardLora.title = !workspace.availableLoras.length
            ? "Refresh after placing LoRAs in ComfyUI/models/loras."
            : "Add an ordered model-only LoRA to this card.";
        refreshCardLoras.disabled = Boolean(workspace.state?.pending_operation);
    }

    async function changeCardReference(file) {
        const project = workspace.state.project;
        const cardId = selectedCard()?.id;
        if (workspace.referenceBusy || !editable(selectedCard())) return;
        if (!await flush()) return;
        if (project !== workspace.state.project || cardId !== selectedCard()?.id) return;
        const epoch = workspace.loadEpoch;
        workspace.referenceBusy = true; render();
        try {
            let options;
            if (file) {
                const body = new FormData();
                body.append("project", project); body.append("card_id", cardId);
                body.append("revision", String(workspace.state.revision)); body.append("image", file);
                options = {method: "POST", body};
            } else {
                options = {method: "DELETE", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({project, card_id: cardId, revision: workspace.state.revision})};
            }
            const body = await responseJson(await api.fetchApi("/longcaster/cards/reference", options));
            if (project === workspace.state.project && epoch === workspace.loadEpoch) {
                updateState(body); setMessage(body.message);
            }
        } catch (error) {
            if (project === workspace.state.project && epoch === workspace.loadEpoch) setMessage(`Card reference: ${error.message}`, true);
        } finally { workspace.referenceBusy = false; render(); }
    }

    function markDirty(sectionNameChanged = null) {
        workspace.dirty = true; workspace.editVersion += 1;
        if (sectionNameChanged) workspace.dirtySections.add(sectionNameChanged);
        clearTimeout(workspace.timer);
        workspace.timer = setTimeout(() => save(), 500);
        renderSaveState();
    }

    async function save() {
        if (workspace.saving) return workspace.saving;
        const card = selectedCard();
        if (!workspace.dirty || !editable(card)) return true;
        clearTimeout(workspace.timer);
        const version = workspace.editVersion;
        const changed = [...workspace.dirtySections];
        const payload = {
            project: workspace.state.project, card_id: card.id, revision: workspace.state.revision,
            sections: Object.fromEntries(changed.map((name) => [name, card.prompt_sections[name].text])),
            duration_seconds: Number(card.requested_duration_seconds), seed: Number(card.seed),
            ref_image_size: card.ref_image_size || "match",
            card_reference_settings: workspace.state.generation_mode === "ref2va" ? referenceSettings(card.card_reference) : null,
            continuation_strategy: card.continuation_strategy,
            color_anchor_source_card_id: card.color_anchor_source_card_id,
            identity_anchor_overrides: card.identity_anchor_overrides || {},
            lora_stack: card.lora_stack || [],
            refine_enabled: Boolean(card.refine_enabled),
        };
        workspace.dirty = false; workspace.dirtySections.clear();
        workspace.saving = (async () => {
            try {
                const body = await responseJson(await api.fetchApi("/longcaster/cards/card", {
                    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
                }));
                if (workspace.editVersion === version) updateState(body);
                else { workspace.state.revision = body.revision; workspace.dirty = true; workspace.timer = setTimeout(() => save(), 100); }
                setMessage("Card saved.");
                return true;
            } catch (error) {
                workspace.dirty = true; changed.forEach((name) => workspace.dirtySections.add(name));
                setMessage(`Save failed: ${error.message}`, true); renderSaveState(); return false;
            } finally { workspace.saving = null; renderSaveState(); }
        })();
        renderSaveState();
        return workspace.saving;
    }

    async function flushCard() {
        clearTimeout(workspace.timer);
        if (workspace.saving && !await workspace.saving) return false;
        if (workspace.dirty) return save();
        return true;
    }

    async function flush() {
        if (workspace.referenceBusy) { setMessage("Wait for the card reference upload to finish."); return false; }
        if (!await flushCard()) return false;
        if (workspace.loraDirty && !await saveProjectLoraWords(true)) return false;
        if (workspace.templatesDirty) return savePromptTemplates(true);
        return true;
    }
    workspace.flush = flush;

    function load(project = widget(node, "project_name")?.value) {
        const loadEpoch = ++workspace.loadEpoch;
        workspace.requestedProject = project;
        workspace.loading = (async () => {
            try {
                const body = await responseJson(await api.fetchApi(`/longcaster/cards/state?project=${encodeURIComponent(project || "")}`));
                if (loadEpoch !== workspace.loadEpoch) return false;
                updateState(body, !workspace.followActive); workspace.followActive = false;
                if (!workspace.loraCatalogLoaded) await loadCardLoraCatalog();
                projectSelect.value = body.project; setMessage(body.message || "Project loaded.");
                return true;
            } catch (error) {
                if (loadEpoch === workspace.loadEpoch) {
                    workspace.requestedProject = workspace.state?.project;
                    if (workspace.state) {
                        syncProjectNode(node, workspace.state);
                        projectSelect.value = workspace.state.project;
                    }
                    setMessage(`Load failed: ${error.message}`, true);
                }
                return false;
            } finally {
                if (loadEpoch === workspace.loadEpoch) workspace.loading = null;
            }
        })();
        return workspace.loading;
    }
    workspace.load = load;
    workspace.refresh = async (project) => {
        if (workspace.state?.project !== project || workspace.requestedProject !== project) return;
        if (!await flush()) return;
        if (workspace.state?.project === project && workspace.requestedProject === project) await load(project);
    };

    function cancelBatch(message = "Batch rendering cancelled.", error = false) {
        if (!workspace.batch?.active) return false;
        workspace.batch.active = false;
        workspace.batch = null;
        setMessage(message, error);
        render();
        return true;
    }
    workspace.cancelBatch = cancelBatch;

    function cancelDraftBatch(message = "Draft queue cancelled.", error = false) {
        if (!workspace.draftBatch?.active) return false;
        workspace.draftBatch.active = false;
        workspace.draftBatch = null;
        setMessage(message, error);
        render();
        return true;
    }
    workspace.cancelDraftBatch = cancelDraftBatch;

    function requestedDraftCount() {
        const count = Number(draftCount.value);
        if (!Number.isInteger(count) || count < 1 || count > 100) {
            setMessage("Draft count must be a whole number from 1 to 100.", true);
            draftCount.focus();
            return null;
        }
        return count;
    }

    async function queueDraftStep() {
        const batch = workspace.draftBatch;
        if (!batch?.active) return;
        const card = selectedCard();
        if (workspace.state?.project !== batch.project || card?.id !== batch.cardId) {
            cancelDraftBatch("Draft queue stopped because the active card changed.", true);
            return;
        }
        if (batch.completed > 0 && card.status !== "DRAFT") {
            cancelDraftBatch("Draft queue stopped because the previous take is unavailable.", true);
            return;
        }
        const action = batch.completed === 0 ? batch.firstAction : "retry";
        const nextSeed = differentUnusedSeed(card.seed, batch.usedSeeds);
        card.seed = nextSeed;
        seed.value = String(nextSeed);
        markDirty();
        if (!await flush()) {
            cancelDraftBatch("Draft queue stopped because its seed could not be saved.", true);
            return;
        }
        if (!batch.active) return;
        batch.expectedAction = action;
        setMessage(`Queueing draft ${batch.completed + 1} of ${batch.count} with seed ${nextSeed}…`);
        const queued = await queueAction(node, action);
        if (queued === null) cancelDraftBatch("Draft queue stopped because the next take could not be queued.", true);
    }

    async function advanceDraftBatch(completedAction) {
        const batch = workspace.draftBatch;
        if (!batch?.active || batch.advancing || completedAction !== batch.expectedAction) return;
        batch.advancing = true;
        try {
            if (!await load(batch.project) || !batch.active) {
                cancelDraftBatch("Draft queue stopped because the project could not be refreshed.", true);
                return;
            }
            const card = selectedCard();
            if (card?.id !== batch.cardId || card.status !== "DRAFT" || card.draft_inputs_dirty) {
                cancelDraftBatch("Draft queue stopped because the draft was not saved correctly.", true);
                return;
            }
            batch.completed += 1;
            if (batch.completed === batch.count) {
                workspace.draftBatch = null;
                setMessage(`Queued and completed ${batch.count} draft${batch.count === 1 ? "" : "s"}. Review the saved takes.`);
                render();
                return;
            }
            await queueDraftStep();
        } catch (error) {
            cancelDraftBatch(`Draft queue stopped: ${error.message}`, true);
        } finally {
            if (workspace.draftBatch === batch) batch.advancing = false;
        }
    }
    workspace.advanceDraftBatch = advanceDraftBatch;

    async function startDraftBatch(firstAction) {
        if (workspace.draftBatch?.active || workspace.batch?.active) return;
        const count = requestedDraftCount();
        if (count === null || !await flush()) return;
        const card = selectedCard();
        if (!card || card.id !== workspace.state.active_card_id || workspace.state.pending_operation
            || (firstAction === "generate" ? !["EMPTY", "FAILED", "INVALIDATED"].includes(card.status) : card.status !== "DRAFT")) {
            setMessage("This card is not ready to queue drafts.", true);
            return;
        }
        workspace.draftBatch = {
            active: true, advancing: false, project: workspace.state.project, cardId: card.id,
            firstAction, count, completed: 0, expectedAction: null,
            usedSeeds: new Set([Number(card.seed), ...(card.draft_takes || []).map((take) => Number(take.seed))]),
        };
        render();
        try { await queueDraftStep(); }
        catch (error) { cancelDraftBatch(`Draft queue stopped: ${error.message}`, true); }
    }

    async function queueBatchStep(action) {
        const batch = workspace.batch;
        if (!batch?.active) return false;
        batch.expectedAction = action;
        workspace.followActive = true;
        setMessage(
            `Batch ${action === "accept" ? "accepting" : "rendering"} Card ${batch.currentIndex + 1} of ${batch.lastIndex + 1}…`
        );
        render();
        const queued = await queueAction(node, action);
        if (queued === null) {
            cancelBatch("Batch rendering stopped because the next operation could not be queued.", true);
            return false;
        }
        return true;
    }

    async function advanceBatch(completedAction) {
        const batch = workspace.batch;
        if (!batch?.active || batch.advancing || completedAction !== batch.expectedAction) return;
        batch.advancing = true;
        try {
            if (workspace.state?.project !== batch.project || !await load(batch.project)) {
                cancelBatch("Batch rendering stopped because its project could not be refreshed.", true);
                return;
            }
            const state = workspace.state;
            const activeCard = state.cards.find((card) => card.id === state.active_card_id);
            if (!activeCard) {
                cancelBatch("Batch rendering stopped because the active card is unavailable.", true);
                return;
            }
            batch.currentIndex = activeCard.timeline_index;
            if (completedAction === "generate") {
                if (activeCard.id !== batch.expectedCardId || activeCard.status !== "DRAFT") {
                    cancelBatch(`Batch rendering stopped: Card ${activeCard.timeline_index + 1} did not produce a draft.`, true);
                    return;
                }
                batch.rendered += 1;
                if (activeCard.id === batch.lastCardId) {
                    const rendered = batch.rendered;
                    workspace.batch = null;
                    setMessage(`Batch complete. Rendered ${rendered} card${rendered === 1 ? "" : "s"}; the final card remains a draft for review.`);
                    render();
                    return;
                }
                if (activeCard.draft_inputs_dirty) {
                    cancelBatch(`Batch rendering stopped: Card ${activeCard.timeline_index + 1} changed after rendering.`, true);
                    return;
                }
                await queueBatchStep("accept");
                return;
            }
            if (completedAction === "accept") {
                const acceptedCard = state.cards.find((card) => card.id === batch.expectedCardId);
                if (acceptedCard?.status !== "ACCEPTED") {
                    cancelBatch("Batch rendering stopped because the intermediate draft was not accepted.", true);
                    return;
                }
                if (!["EMPTY", "FAILED", "INVALIDATED"].includes(activeCard.status)) {
                    cancelBatch("Batch rendering stopped because acceptance did not activate the next draft card.", true);
                    return;
                }
                batch.expectedCardId = activeCard.id;
                await queueBatchStep("generate");
            }
        } finally {
            if (workspace.batch) workspace.batch.advancing = false;
        }
    }
    workspace.advanceBatch = advanceBatch;

    async function startBatchRender() {
        if (workspace.batch?.active) {
            cancelBatch("Batch will stop after the current operation. The current operation was not interrupted.");
            return;
        }
        if (!await flush()) return;
        const plan = remainingBatchPlan(workspace.state);
        if (!plan) {
            setMessage("Batch rendering requires the active card followed only by invalidated cards.", true);
            return;
        }
        const activeCard = workspace.state.cards.find((card) => card.id === plan.firstCardId);
        const finalCard = workspace.state.cards.find((card) => card.id === plan.lastCardId);
        const warning = `Render ${plan.renderCount} remaining card${plan.renderCount === 1 ? "" : "s"} from Card ${activeCard.timeline_index + 1} through Card ${finalCard.timeline_index + 1}? Intermediate drafts will be accepted automatically, including any configured Optional Source Refine. Drift/Color-Stable settings remain active. The final card will remain a draft for review. Keep LongCaster Studio open while the batch runs.`;
        if (!confirm(warning)) return;
        workspace.selectedCardId = activeCard.id;
        workspace.viewedTakeId = activeCard.selected_draft_take_id || null;
        workspace.batch = {
            active: true,
            advancing: false,
            project: workspace.state.project,
            lastCardId: plan.lastCardId,
            lastIndex: finalCard.timeline_index,
            currentIndex: activeCard.timeline_index,
            expectedCardId: activeCard.id,
            expectedAction: null,
            rendered: 0,
        };
        await queueBatchStep(plan.firstAction);
    }

    workspace.switchProject = async (project) => {
        cancelBatch("Batch rendering cancelled because the project changed.");
        cancelDraftBatch("Draft queue cancelled because the project changed.");
        if (!await flush()) {
            if (workspace.state) {
                syncProjectNode(node, workspace.state);
                projectSelect.value = workspace.state.project;
            }
            return false;
        }
        return load(project);
    };

    async function loadProjects(preferredProject = null, loadSelected = true) {
        const projectListEpoch = ++workspace.projectListEpoch;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/projects"));
            if (projectListEpoch !== workspace.projectListEpoch) return false;
            const current = preferredProject || widget(node, "project_name")?.value;
            projectSelect.replaceChildren();
            for (const project of body.projects || []) {
                const option = document.createElement("option"); option.value = project.project; option.textContent = `${project.project} (${project.card_count})`; projectSelect.append(option);
            }
            const selected = body.projects?.some((item) => item.project === current) ? current : body.projects?.[0]?.project;
            if (selected) { projectSelect.value = selected; if (loadSelected) await load(selected); }
            else setMessage("No LongCaster projects found.", true);
            return true;
        } catch (error) {
            if (projectListEpoch === workspace.projectListEpoch) setMessage(`Project refresh failed: ${error.message}`, true);
            return false;
        }
    }

    async function createNewProject() {
        workspace.loadEpoch += 1;
        workspace.loading = null;
        workspace.projectListEpoch += 1;
        try {
            const resolution = selectedNewResolution();
            const body = await responseJson(await api.fetchApi("/longcaster/cards/projects", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    project: newName.value.trim(), generation_mode: newMode.value,
                    width: resolution.width, height: resolution.height,
                    duration_seconds: Number(newDuration.value), seed: Number(newSeed.value),
                    refine_cadence: String(widget(node, "refine_cadence")?.value || "off"),
                    continuation_treatment: String(widget(node, "continuation_treatment")?.value || "off"),
                }),
            }));
            newPanel.hidden = true;
            workspace.selectedCardId = null;
            updateState(body, false);
            await loadProjects(body.project, false);
            projectSelect.value = body.project;
            setMessage("Project created. Configure its first card, then generate.");
        } catch (error) { setMessage(`Create failed: ${error.message}`, true); }
    }

    async function duplicateCurrentProject(invalidateRenders = false) {
        if (!await flush()) return;
        const source = workspace.state?.project;
        if (!source) return;
        const requested = globalThis.prompt(
            `${invalidateRenders ? "Duplicate and invalidate" : "Duplicate"} ${source} as:`,
            `${source}_${invalidateRenders ? "rerender" : "copy"}`
        );
        if (requested === null) return;
        const destination = requested.trim();
        if (!destination) {
            setMessage("Duplicate project name is required.", true);
            return;
        }
        const activeButton = invalidateRenders ? duplicateAndInvalidate : duplicateProject;
        activeButton.disabled = true;
        activeButton.textContent = "Duplicating…";
        setMessage(`${invalidateRenders ? "Duplicating card definitions" : "Duplicating"} ${source} as ${destination}…`);
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/projects/duplicate", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    source_project: source,
                    project: destination,
                    invalidate_renders: invalidateRenders,
                }),
            }));
            workspace.selectedCardId = null;
            workspace.loraDirty = false;
            workspace.templatesDirty = false;
            updateState(body, false);
            await loadProjects(body.project, false);
            projectSelect.value = body.project;
            setMessage(invalidateRenders
                ? `Project duplicated as ${body.project} with all renders invalidated.`
                : `Project duplicated as ${body.project}.`);
        } catch (error) {
            setMessage(`Duplicate failed: ${error.message}`, true);
        } finally {
            activeButton.textContent = invalidateRenders
                ? "Duplicate and Invalidate Project"
                : "Duplicate Project";
            render();
        }
    }

    async function saveProjectMode() {
        const requested = mode.value;
        if (!await flush()) return;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/project", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({project: workspace.state.project, revision: workspace.state.revision, generation_mode: requested}),
            }));
            updateState(body); setMessage("Generation mode saved.");
        } catch (error) { mode.value = workspace.state.generation_mode; setMessage(`Mode change failed: ${error.message}`, true); }
    }

    async function saveResolution() {
        if (!await flush()) return;
        const requested = selectedProjectResolution();
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/project", {
                method: "PATCH", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    generation_mode: workspace.state.generation_mode,
                    width: requested.width,
                    height: requested.height,
                }),
            }));
            updateState(body);
            setMessage(`Project resolution changed to ${body.width} × ${body.height}.`);
        } catch (error) {
            setMessage(`Resolution change failed: ${error.message}`, true);
            render();
        }
    }

    async function saveRefineCadence(requested) {
        if (!await flush()) return;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/project", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    generation_mode: workspace.state.generation_mode,
                    refine_cadence: requested,
                }),
            }));
            updateState(body); setMessage("Continuation refine cadence saved.");
        } catch (error) {
            setRefineRadios(refineCadence, workspace.state.refine_cadence || "off");
            setRefineRadios(subjectRefineCadence, workspace.state.refine_cadence || "off");
            setMessage(`Refine cadence change failed: ${error.message}`, true);
        }
    }

    async function saveContinuationTreatment(requested) {
        if (!await flush()) return;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/project", {
                method: "PATCH", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    generation_mode: workspace.state.generation_mode,
                    continuation_treatment: requested,
                }),
            }));
            updateState(body); setMessage("Continuation treatment saved.");
        } catch (error) {
            treatment.value = workspace.state.continuation_treatment || "off";
            setMessage(`Continuation treatment change failed: ${error.message}`, true);
        }
    }

    async function saveProjectLoraWords(cardAlreadyFlushed = false) {
        if (!cardAlreadyFlushed && !await flushCard()) return false;
        const requested = loraWords.value.trim();
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/project", {
                method: "PATCH", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    generation_mode: workspace.state.generation_mode,
                    lora_activation_words: requested,
                }),
            }));
            workspace.loraDirty = false;
            updateState(body);
            setMessage(requested ? "Project LoRA activation words saved." : "Project LoRA activation words cleared.");
            return true;
        } catch (error) {
            workspace.loraDirty = false;
            loraWords.value = workspace.state.lora_activation_words || "";
            setMessage(`LoRA activation update failed: ${error.message}`, true);
            render();
            return false;
        }
    }

    async function savePromptTemplates(cardAlreadyFlushed = false) {
        if (!cardAlreadyFlushed && !await flushCard()) return false;
        const requested = cloneTemplates(workspace.templateDraft).map((item) => ({
            name: item.name.trim(), text: item.text,
        }));
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/project", {
                method: "PATCH", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    generation_mode: workspace.state.generation_mode,
                    prompt_templates: requested,
                }),
            }));
            workspace.templatesDirty = false;
            workspace.templateDraft = cloneTemplates(body.prompt_templates);
            updateState(body);
            setMessage(requested.length ? "Project prompt templates saved." : "Project prompt templates cleared.");
            return true;
        } catch (error) {
            setMessage(`Prompt template update failed: ${error.message}`, true);
            renderProjectTemplates();
            return false;
        }
    }

    async function setContinuationSource(sourceType) {
        if (!await flush()) return;
        const card = selectedCard();
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/continuation-source", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    card_id: card.id,
                    source_type: sourceType,
                }),
            }));
            updateState(body);
            setMessage(sourceType === "derivative" ? "Refined continuation selected." : "Accepted master selected.");
        } catch (error) { setMessage(`Continuation source update failed: ${error.message}`, true); }
    }

    function identityNode() {
        return node.graph?._nodes?.find((item) => item.comfyClass === "LongCasterIdentityAnchor" || item.type === "LongCasterIdentityAnchor");
    }

    function queueIdentity(action, card) {
        if (!card) {
            setMessage("Choose an accepted identity source card first.", true);
            return;
        }
        const target = identityNode();
        if (!target) {
            setMessage("Add a MiniMax H3 LongCaster Identity Anchor node to this workflow to decode previews or create identities.", true);
            return;
        }
        if (action === "set" && identityScope.value === "custom" && !identityCustom.value.trim()) {
            setMessage("Custom identity scope requires an instruction describing what may be inherited.", true);
            identityCustom.focus();
            return;
        }
        const values = {
            project_name: workspace.state.project,
            source_card: String(card.artifact_number),
            frame_index: Number(identityFrame.value || 0),
            subject_id: identitySubject.value.trim() || "<Subject 1>",
            label: identityLabel.value.trim() || "identity checkpoint",
            identity_scope: identityScope.value,
            custom_identity_instruction: identityScope.value === "custom" ? identityCustom.value.trim() : "",
        };
        for (const [name, value] of Object.entries(values)) { const item = widget(target, name); if (item) item.value = value; }
        const projectAction = widget(node, "action"); if (projectAction) projectAction.value = "resume";
        setMessage(`${action === "build_preview" ? "Preview build" : "Identity extraction"} queued for Card ${card.timeline_index + 1}.`);
        queueAction(target, action);
    }

    async function manageIdentity(action, item) {
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/identity", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    project: workspace.state.project, revision: workspace.state.revision,
                    action, subject_id: item.subject_id, anchor_id: item.anchor_id,
                }),
            }));
            updateState(body); setMessage(body.message || "Identity updated.");
        } catch (error) { setMessage(`Identity update failed: ${error.message}`, true); }
    }

    async function importIdentity(file) {
        if (!file || workspace.state.pending_operation) return;
        if (workspace.state.generation_mode !== "ref2va") {
            setMessage("Identity anchors currently require a REF2VA project.", true);
            return;
        }
        const subjectId = identitySubject.value.trim();
        if (!subjectId) {
            setMessage("Enter a subject before importing an identity image.", true);
            identitySubject.focus();
            return;
        }
        if (identityScope.value === "custom" && !identityCustom.value.trim()) {
            setMessage("Custom identity scope requires an instruction describing what may be inherited.", true);
            identityCustom.focus();
            return;
        }
        if (!await flush()) return;
        const form = new FormData();
        form.append("project", workspace.state.project);
        form.append("revision", String(workspace.state.revision));
        form.append("subject_id", subjectId);
        form.append("label", identityLabel.value.trim() || file.name || "imported identity");
        form.append("identity_scope", identityScope.value);
        form.append("custom_identity_instruction", identityScope.value === "custom" ? identityCustom.value.trim() : "");
        form.append("image", file, file.name || "identity-image");
        identityImport.classList.add("disabled");
        identityImportInput.disabled = true;
        setMessage(`Importing identity image ${file.name || "image"}…`);
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/identity/import", {
                method: "POST", body: form,
            }));
            updateState(body);
            setMessage(body.message || "Identity image imported.");
        } catch (error) {
            setMessage(`Identity import failed: ${error.message}`, true);
        } finally {
            identityImportInput.value = "";
            renderIdentitySource();
        }
    }

    async function manageDraftTake(action, card, take = null) {
        if (!card) return;
        if (action === "select" && (workspace.dirty || workspace.saving || card.draft_inputs_dirty) && !confirm(
            "Select this saved take and restore its prompt and seed? Current unrendered prompt or seed changes will be discarded."
        )) return;
        if (!await flush()) return;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/take", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    card_id: card.id,
                    take_id: take?.id,
                    action,
                }),
            }));
            workspace.viewedTakeId = action === "select"
                ? take.id
                : body.cards.find((item) => item.id === card.id)?.selected_draft_take_id || null;
            updateState(body);
            setMessage(body.message || "Draft takes updated.");
        } catch (error) {
            setMessage(`Draft take update failed: ${error.message}`, true);
        }
    }

    function selectedProjectPreviewClips() {
        const clips = [];
        for (const card of workspace.state?.cards || []) {
            const take = card.draft_takes?.find(
                (item) => item.id === card.selected_draft_take_id
            );
            if (take?.preview_available) {
                clips.push({
                    cardId: card.id,
                    cardNumber: card.timeline_index + 1,
                    takeId: take.id,
                    version: take.preview_version || take.artifact_sha256 || "",
                });
            } else if (card.preview_available) {
                clips.push({
                    cardId: card.id,
                    cardNumber: card.timeline_index + 1,
                    takeId: null,
                    version: card.preview_version || card.updated_at || "",
                });
            }
        }
        return clips;
    }

    function resetProjectPreview() {
        projectPreviewVideo.pause();
        projectPreviewVideo.removeAttribute("src");
        projectPreviewVideo.load();
        projectPreviewVideo.hidden = true;
        workspace.projectPreviewClips = [];
        workspace.projectPreviewIndex = -1;
        playProjectPreview.textContent = "Play Project Preview";
        projectPreviewStatus.textContent = "Plays each selected card preview in timeline order without creating a stitched file.";
    }

    function loadProjectPreviewClip(index, autoplay = true) {
        const clip = workspace.projectPreviewClips[index];
        if (!clip) return;
        workspace.projectPreviewIndex = index;
        const query = new URLSearchParams({
            project: workspace.state.project,
            card: clip.cardId,
            version: clip.version,
        });
        if (clip.takeId) query.set("take", clip.takeId);
        projectPreviewVideo.src = api.apiURL(`/longcaster/cards/preview?${query.toString()}`);
        projectPreviewVideo.hidden = false;
        projectPreviewVideo.load();
        projectPreviewStatus.textContent = `Playing Card ${clip.cardNumber} · clip ${index + 1} of ${workspace.projectPreviewClips.length}`;
        playProjectPreview.textContent = "Restart Project Preview";
        if (autoplay) projectPreviewVideo.play().catch(() => {
            projectPreviewStatus.textContent = `Card ${clip.cardNumber} ready · press play to continue.`;
        });
    }

    function focusReferenceGraph() {
        const input = node.inputs?.find((item) => item.name === "reference_packet");
        const link = input?.link == null ? null : node.graph?.links?.[input.link];
        const source = link ? node.graph?.getNodeById?.(link.origin_id) : null;
        if (!source) {
            setMessage("No MMH3 reference packet is connected to this LongCaster node.", true);
            return;
        }
        root.hidden = true;
        app.canvas?.selectNode?.(source);
        app.canvas?.centerOnNode?.(source);
        node.graph?.setDirtyCanvas(true, true);
    }

    async function copyFrom(sourceId) {
        if (!await flush()) return;
        const card = selectedCard();
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/copy", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({project: workspace.state.project, revision: workspace.state.revision, target_card_id: card.id, source_card_id: sourceId, sections: [workspace.selectedSection]}),
            }));
            updateState(body); setMessage("Prompt section copied.");
        } catch (error) { setMessage(`Copy failed: ${error.message}`, true); }
    }

    async function clearCurrentSection() {
        if (!await flush()) return;
        const card = selectedCard();
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/card", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({project: workspace.state.project, card_id: card.id, revision: workspace.state.revision, clear_sections: [workspace.selectedSection]}),
            }));
            updateState(body); setMessage("Prompt section cleared.");
        } catch (error) { setMessage(`Clear failed: ${error.message}`, true); }
    }

    async function replaceFullPrompt() {
        const pasted = importText.value;
        if (!pasted.trim()) {
            setMessage("Paste a prompt before importing it.", true);
            importText.focus();
            return;
        }
        if (!await flush()) return;
        const card = selectedCard();
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/card", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    project: workspace.state.project, card_id: card.id,
                    revision: workspace.state.revision, import_prompt: pasted,
                }),
            }));
            const imported = body.cards.find((item) => item.id === card.id);
            workspace.selectedSection = CARD_SECTIONS.find(([name]) => imported.prompt_sections[name].text)?.[0] || "detailed_description";
            workspace.importCardId = null;
            importPanel.hidden = true;
            fullPrompt.hidden = false;
            updateState(body);
            setMessage("Full prompt imported and split into sections.");
        } catch (error) { setMessage(`Prompt import failed: ${error.message}`, true); }
    }

    async function runAction(action, queuedMessage = `${action} queued…`) {
        if (workspace.loading && !await workspace.loading) return;
        if (!await flush()) return;
        const card = selectedCard();
        if (action === "accept" && card.draft_inputs_dirty) {
            setMessage("Draft inputs changed. Retry Draft before accepting.", true);
            render();
            return;
        }
        syncProjectNode(node, workspace.state);
        workspace.followActive = ["append", "accept", "invalidate_all"].includes(action);
        setMessage(queuedMessage); queueAction(node, action);
    }

    async function invalidateFromSelectedCard() {
        if (!await flush()) return;
        const card = selectedCard();
        if (!card) return;
        const count = workspace.state.cards.length - card.timeline_index;
        const warning = `Invalidate Card ${card.timeline_index + 1} and ${count - 1} later card${count - 1 === 1 ? "" : "s"}? This permanently deletes render artifacts for ${count} card${count === 1 ? "" : "s"}, including masters, draft takes, previews, refinements, and anchors. Prompts, card settings, and the accepted prefix will remain. Card ${card.timeline_index + 1} will become the active card for regeneration.`;
        if (!confirm(warning)) return;
        try {
            setMessage("Invalidating this card and its tail…");
            const body = await responseJson(await api.fetchApi("/longcaster/cards/invalidate-tail", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    project: workspace.state.project,
                    revision: workspace.state.revision,
                    card_id: card.id,
                }),
            }));
            workspace.selectedCardId = card.id;
            workspace.viewedTakeId = null;
            updateState(body);
            setMessage(body.message || "Card tail invalidated.");
        } catch (error) {
            setMessage(`Tail invalidation failed: ${error.message}`, true);
        }
    }

    function retryWithDifferentSeed() {
        const card = selectedCard();
        if (!card || card.status !== "DRAFT") return;
        const count = requestedDraftCount();
        if (count === null) return;
        if (count > 1) { startDraftBatch("retry"); return; }
        const nextSeed = differentRandomSeed(card.seed);
        card.seed = nextSeed;
        seed.value = String(nextSeed);
        markDirty();
        runAction("retry", `Retry with new seed ${nextSeed} queued…`);
    }

    textarea.oninput = () => {
        const card = selectedCard();
        card.prompt_sections[workspace.selectedSection].text = textarea.value;
        card.assembled_prompt = CARD_SECTIONS.map(([name]) => `${name}:${card.prompt_sections[name].text ? `\n${card.prompt_sections[name].text}` : ""}`).join("\n\n");
        fullPrompt.textContent = card.assembled_prompt;
        markDirty(workspace.selectedSection);
        renderPromptDiff(card, viewedTake(card));
    };
    textarea.onscroll = () => {
        const card = selectedCard(); if (!card || card.prompt_format !== "structured_v1") return;
        const start = card.assembled_prompt.indexOf(`${workspace.selectedSection}:`);
        const sectionRatio = textarea.scrollTop / Math.max(1, textarea.scrollHeight - textarea.clientHeight);
        const ratio = Math.max(0, start) / Math.max(1, card.assembled_prompt.length) + sectionRatio / CARD_SECTIONS.length;
        fullPrompt.scrollTop = Math.min(1, ratio) * Math.max(0, fullPrompt.scrollHeight - fullPrompt.clientHeight);
    };
    duration.oninput = () => { selectedCard().requested_duration_seconds = Number(duration.value); markDirty(); };
    seed.oninput = () => { selectedCard().seed = Number(seed.value); markDirty(); };
    refImageSize.onchange = () => { selectedCard().ref_image_size = refImageSize.value; markDirty(); };
    pictureToggle.onclick = () => {
        workspace.pictureRailOpen = !workspace.pictureRailOpen;
        try { globalThis.localStorage?.setItem("longcaster.pictureRailOpen", String(workspace.pictureRailOpen)); } catch (_) {}
        render();
    };
    strategy.onchange = () => {
        const card = selectedCard();
        card.continuation_strategy = strategy.value;
        if (strategy.value === "direct_mmh3") {
            const predecessor = workspace.state.cards.find((item) => item.id === card.timeline_predecessor_id);
            card.color_anchor_source_card_id = predecessor?.color_anchor_source_card_id || predecessor?.id || card.id;
        } else {
            card.color_anchor_source_card_id = card.id;
        }
        markDirty();
        render();
    };
    colorBaseline.onchange = () => { selectedCard().color_anchor_source_card_id = colorBaseline.value; markDirty(); };
    mode.onchange = () => saveProjectMode();
    treatment.onchange = () => saveContinuationTreatment(treatment.value);
    projectAspect.onchange = () => renderProjectResolutionChoice();
    projectMegapixels.oninput = () => renderProjectResolutionChoice();
    saveProjectResolution.onclick = () => saveResolution();
    invalidateAllRenders.onclick = () => {
        const warning = "Invalidate ALL card renders? All renders will be lost. Every MMH3 master and draft take, preview, refinement, anchor, and superseded publication in this project will be permanently deleted. All prompts and card definitions will remain, and regeneration will restart from Card 1.";
        if (confirm(warning)) runAction("invalidate_all", "Invalidating all project renders…");
    };
    for (const group of [refineCadence, subjectRefineCadence]) {
        for (const input of Object.values(group.inputs)) input.onchange = () => {
            if (input.checked) saveRefineCadence(input.value);
        };
    }
    const updateRefineEnabled = (checked) => {
        const card = selectedCard();
        if (!card) return;
        card.refine_enabled = Boolean(checked);
        refineEnabled.checked = card.refine_enabled;
        subjectRefineEnabled.checked = card.refine_enabled;
        markDirty();
    };
    refineEnabled.onchange = () => updateRefineEnabled(refineEnabled.checked);
    subjectRefineEnabled.onchange = () => updateRefineEnabled(subjectRefineEnabled.checked);
    loraWords.oninput = () => {
        workspace.loraDirty = loraWords.value.trim() !== (workspace.state?.lora_activation_words || "");
        saveLoraWords.disabled = Boolean(workspace.state?.pending_operation)
            || !workspace.loraDirty;
        renderSaveState();
    };
    addTemplate.onclick = () => {
        if (workspace.templateDraft.length >= 64 || workspace.state?.pending_operation) return;
        workspace.templateDraft.push({name: "", text: ""});
        markTemplatesDirty();
        renderProjectTemplates();
        templateList.querySelector(".lc-template-row:last-child input")?.focus();
    };
    saveTemplates.onclick = () => savePromptTemplates();
    addCardLora.onclick = () => {
        const card = selectedCard();
        if (!editable(card) || !workspace.availableLoras.length) return;
        card.lora_stack ||= [];
        if (card.lora_stack.length >= 8) return;
        card.lora_stack.push({
            lora_name: workspace.availableLoras[0], strength_model: 1.0,
            activation_words: "", enabled: true,
        });
        markDirty();
        renderCardLoras(card, true);
    };
    refreshCardLoras.onclick = () => loadCardLoraCatalog(true);
    saveLoraWords.onclick = () => saveProjectLoraWords();
    identityScope.onchange = () => { customScopeField.hidden = identityScope.value !== "custom"; };
    identityImport.onclick = () => { if (!identityImportInput.disabled) identityImportInput.click(); };
    identityImportInput.onclick = (event) => event.stopPropagation();
    identityImport.onkeydown = (event) => {
        if ((event.key === "Enter" || event.key === " ") && !identityImportInput.disabled) {
            event.preventDefault();
            identityImportInput.click();
        }
    };
    identityImportInput.onchange = () => importIdentity(identityImportInput.files?.[0]);
    identityImport.ondragover = (event) => {
        event.preventDefault();
        if (!identityImportInput.disabled) identityImport.classList.add("dragging");
    };
    identityImport.ondragleave = () => identityImport.classList.remove("dragging");
    identityImport.ondrop = (event) => {
        event.preventDefault();
        identityImport.classList.remove("dragging");
        if (!identityImportInput.disabled) importIdentity(event.dataTransfer?.files?.[0]);
    };
    const syncIdentityPreviewFrame = () => { if (identityVideo.src) identityFrame.value = String(Math.max(0, Math.round(identityVideo.currentTime * 24))); };
    identityVideo.addEventListener("pause", syncIdentityPreviewFrame);
    identityVideo.addEventListener("seeked", syncIdentityPreviewFrame);
    copyPrevious.onclick = () => copyFrom(selectedCard().timeline_predecessor_id);
    copySelected.onclick = () => copyFrom(sourceSelect.value);
    clearSection.onclick = () => clearCurrentSection();
    importFull.onclick = async () => {
        if (!await flush()) return;
        workspace.importCardId = selectedCard().id;
        const card = selectedCard();
        const otherText = CARD_SECTIONS.some(([name]) => name !== "detailed_description" && card.prompt_sections[name].text);
        const embedded = card.prompt_sections?.detailed_description?.text || "";
        importText.value = !otherText && /(?:^|\n)\s*(?:subject[_ -]+definitions|summary|retention[_ -]+analysis)\s*:/i.test(embedded) ? embedded : "";
        importPanel.hidden = false;
        fullPrompt.hidden = true;
        importText.focus();
    };
    applyImport.onclick = () => replaceFullPrompt();
    cancelImport.onclick = () => { workspace.importCardId = null; importPanel.hidden = true; fullPrompt.hidden = false; };
    convertLegacy.onclick = async () => {
        const card = selectedCard();
        if (!confirm("Convert this flat prompt now? Recognized headings will be assigned automatically. If they cannot be identified safely, the full text will go into Detailed Description.")) return;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/card", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({project: workspace.state.project, card_id: card.id, revision: workspace.state.revision, convert_legacy: true}),
            }));
            const converted = body.cards.find((item) => item.id === card.id);
            workspace.selectedSection = CARD_SECTIONS.find(([name]) => converted.prompt_sections[name].text)?.[0] || "detailed_description";
            updateState(body); setMessage("Flat prompt converted and the historical panel was removed.");
        } catch (error) { setMessage(`Conversion failed: ${error.message}`, true); }
    };
    previousCard.onclick = async () => { const card = selectedCard(); const target = workspace.state.cards[card.timeline_index - 1]; if (target && await flush()) { workspace.selectedCardId = target.id; workspace.viewedTakeId = target.selected_draft_take_id || null; render(); } };
    nextCard.onclick = async () => { const card = selectedCard(); const target = workspace.state.cards[card.timeline_index + 1]; if (target && await flush()) { workspace.selectedCardId = target.id; workspace.viewedTakeId = target.selected_draft_take_id || null; render(); } };
    cardViewTab.onclick = async () => { if (await flush()) { workspace.activeView = "card"; render(); } };
    projectViewTab.onclick = async () => { if (await flush()) { workspace.activeView = "project"; render(); } };
    identitySourceSelect.onchange = () => {
        workspace.identitySourceCardId = identitySourceSelect.value;
        identityFrame.value = "0";
        renderIdentitySource();
    };
    generate.onclick = () => {
        const count = requestedDraftCount();
        if (count === null) return;
        if (count > 1) startDraftBatch("generate");
        else runAction("generate");
    };
    queueDrafts.onclick = () => startDraftBatch("generate");
    retry.onclick = () => runAction("retry");
    retryDifferentSeed.onclick = () => retryWithDifferentSeed();
    accept.onclick = () => runAction("accept");
    batchRender.onclick = () => startBatchRender();
    append.onclick = () => runAction("append");
    unpublish.onclick = () => {
        const card = selectedCard();
        if (card?.continuation_refine && !confirm("Unpublish this card? Its immutable master will be retained, but all refine derivatives for the card will be permanently removed.")) return;
        runAction("unpublish");
    };
    invalidateRender.onclick = () => {
        invalidateFromSelectedCard();
    };
    createRefine.onclick = () => runAction("create_refine");
    useAcceptedMaster.onclick = () => setContinuationSource("accepted_master");
    useRefined.onclick = () => setContinuationSource("derivative");
    removeDraft.onclick = () => {
        const card = selectedCard();
        const warning = card?.has_publication_history
            ? "Permanently remove this draft card and all of its superseded accepted publications, refine derivatives, anchors, and previews? The previous accepted card will become active."
            : "Remove this unaccepted draft card and return to the previous accepted card? Its draft render and preview will be discarded.";
        if (confirm(warning)) runAction("remove_draft");
    };
    newProject.onclick = () => { newPanel.hidden = !newPanel.hidden; if (!newPanel.hidden) newName.focus(); };
    duplicateProject.onclick = () => duplicateCurrentProject();
    duplicateAndInvalidate.onclick = () => duplicateCurrentProject(true);
    cancelCreate.onclick = () => { newPanel.hidden = true; };
    createProject.onclick = () => createNewProject();
    buildPreview.onclick = () => queueIdentity("build_preview", selectedCard());
    deleteUnselectedTakes.onclick = () => {
        const card = selectedCard();
        const count = Math.max(0, (card?.draft_takes?.length || 0) - 1);
        if (count && confirm(`Permanently delete ${count} unselected draft take${count === 1 ? "" : "s"} and their previews?`)) {
            manageDraftTake("delete_unselected", card);
        }
    };
    buildIdentityPreview.onclick = () => queueIdentity("build_preview", identitySourceCard());
    createIdentity.onclick = () => queueIdentity("set", identitySourceCard());
    refreshIdentities.onclick = async () => { if (await flush()) await load(workspace.state.project); };
    refreshReferences.onclick = async () => { if (await flush()) { await load(workspace.state.project); renderReferences(selectedCard()); } };
    editReferences.onclick = () => focusReferenceGraph();
    exportProjectPrompts.onclick = async () => {
        if (!await flush()) return;
        const link = document.createElement("a");
        link.href = api.apiURL(`/longcaster/cards/prompts/export?project=${encodeURIComponent(workspace.state.project)}`);
        link.download = "";
        document.body.append(link);
        link.click();
        link.remove();
        setMessage("Project prompt export started.");
    };
    playProjectPreview.onclick = async () => {
        if (!await flush()) return;
        workspace.projectPreviewClips = selectedProjectPreviewClips();
        if (!workspace.projectPreviewClips.length) {
            resetProjectPreview();
            setMessage("No selected card previews are available.", true);
            return;
        }
        loadProjectPreviewClip(0);
    };
    projectPreviewVideo.addEventListener("ended", () => {
        const next = workspace.projectPreviewIndex + 1;
        if (next < workspace.projectPreviewClips.length) {
            loadProjectPreviewClip(next);
            return;
        }
        const count = workspace.projectPreviewClips.length;
        projectPreviewStatus.textContent = `Project preview complete · ${count} clip${count === 1 ? "" : "s"} played.`;
        playProjectPreview.textContent = "Replay Project Preview";
    });
    projectPreviewVideo.addEventListener("error", () => {
        const clip = workspace.projectPreviewClips[workspace.projectPreviewIndex];
        projectPreviewStatus.textContent = `Could not play Card ${clip?.cardNumber ?? "?"}'s selected preview.`;
    });
    projectSelect.onchange = () => workspace.switchProject(projectSelect.value);
    refresh.onclick = async () => { if (await flush()) await loadProjects(); }; close.onclick = async () => { if (await flush()) { cancelDraftBatch("Draft queue stopped because the interface closed."); root.hidden = true; } };
    window.addEventListener("beforeunload", (event) => { if (workspace.dirty || workspace.loraDirty || workspace.templatesDirty) { event.preventDefault(); event.returnValue = ""; } });
    loadProjects();
}

app.registerExtension({
    name: "minimax.h3.longcaster",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === "VHS_VideoCombine") {
            const originalCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const result = originalCreated?.apply(this, arguments);
                setTimeout(() => ensureLongCasterVhsPlayer(this), 0);
                return result;
            };
            const originalConfigured = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const result = originalConfigured?.apply(this, arguments);
                setTimeout(() => ensureLongCasterVhsPlayer(this), 0);
                return result;
            };
            const originalExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                originalExecuted?.apply(this, arguments);
                loadLongCasterVhsResult(this, message);
            };
            return;
        }
        if (nodeData.name === "LongCasterIdentityAnchor") {
            const originalCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const result = originalCreated?.apply(this, arguments);
                const action = widget(this, "action");
                if (action) action.value = "inspect";
                hideWidget(widget(this, "action"));
                hideWidget(widget(this, "source_card"));
                hideWidget(widget(this, "frame_index"));
                hideWidget(widget(this, "command_id"));
                createIdentityPicker(this);
                const computed = this.computeSize?.();
                this.setSize([Math.max(this.size[0], 470), Math.max(this.size[1], computed?.[1] ?? this.size[1])]);
                setTimeout(() => refreshIdentityProjects(this), 0);
                return result;
            };

            const originalExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                originalExecuted?.apply(this, arguments);
                const state = message?.longcaster_identity?.[0];
                if (!state) return;
                if (state.project !== widget(this, "project_name")?.value) return;
                const action = widget(this, "action");
                if (action) action.value = "inspect";
                updateIdentityPanel(this, state);
                for (const projectNode of this.graph?._nodes || []) {
                    const workspace = projectNode.longcasterCardsWorkspace;
                    if (workspace?.state?.project === state.project && (!workspace.requestedProject || workspace.requestedProject === state.project)) {
                        workspace.refresh(state.project);
                    }
                }
            };

            const originalForeground = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                originalForeground?.apply(this, arguments);
                const source = this.longcasterIdentityState?.active_source;
                if (!source || this.flags.collapsed) return;
                ctx.save();
                ctx.font = "12px sans-serif";
                ctx.fillStyle = source.enabled ? "#8fd18f" : "#bbb";
                ctx.textAlign = "right";
                ctx.fillText(`Card ${source.card ?? "?"} · frame ${source.preview_frame_index ?? "?"}`, this.size[0] - 10, -7);
                ctx.restore();
            };
            return;
        }
        if (nodeData.name === "LongCasterRegisterPreview") {
            const originalExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                originalExecuted?.apply(this, arguments);
                const preview = message?.longcaster_preview?.[0];
                if (!preview) return;
                const authority = projectAuthority(this);
                const workspace = authority?.longcasterCardsWorkspace;
                if (workspace?.state?.project !== preview.project) return;
                if (workspace.requestedProject && workspace.requestedProject !== preview.project) return;
                workspace.refresh(preview.project);
            };
            return;
        }
        if (nodeData.name !== "LongCasterProject") return;

        const originalCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalCreated?.apply(this, arguments);
            const action = widget(this, "action");
            if (action) action.value = "resume";
            const project = widget(this, "project_name");
            if (project) {
                const originalCallback = project.callback;
                project.callback = (...args) => {
                    originalCallback?.apply(project, args);
                    this.longcasterCardsWorkspace?.switchProject(project.value);
                };
            }
            this.addWidget("button", "Open Cards Interface", null, () => createCardsWorkspace(this));
            this.addWidget("button", "Resume Project", null, () => queueAction(this, "resume"));
            this.addWidget("button", "Stop Render / Unlock", null, () => stopAndUnlock(this));
            this.addWidget("button", "Generate Draft", null, () => queueAction(this, "generate"));
            this.addWidget("button", "Retry Draft", null, () => queueAction(this, "retry"));
            this.addWidget("button", "Accept Draft", null, () => queueAction(this, "accept"));
            this.addWidget("button", "Unpublish Latest Card", null, () => queueAction(this, "unpublish"));
            this.addWidget("button", "Append Card", null, () => queueAction(this, "append"));
            const computed = this.computeSize?.();
            this.setSize([Math.max(this.size[0], 360), Math.max(this.size[1], computed?.[1] ?? this.size[1])]);
            return result;
        };

        const originalConfigured = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = originalConfigured?.apply(this, arguments);
            repairContinuationTreatmentWidgetOrder(this);
            return result;
        };

        const originalExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            originalExecuted?.apply(this, arguments);
            const state = message?.longcaster_state?.[0];
            if (!state) return;
            const workspace = this.longcasterCardsWorkspace;
            if (state.project !== widget(this, "project_name")?.value) return;
            if (workspace?.requestedProject && workspace.requestedProject !== state.project) return;
            syncProjectNode(this, state);
            const action = widget(this, "action");
            if (action) action.value = "resume";
            if (workspace) {
                workspace.refresh(state.project);
            }
            this.graph?.setDirtyCanvas(true, true);
        };

        const originalForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            originalForeground?.apply(this, arguments);
            const state = this.longcasterState;
            if (!state || this.flags.collapsed) return;
            const card = state.active_card;
            ctx.save();
            ctx.font = "12px sans-serif";
            ctx.fillStyle = card?.status === "ACCEPTED" ? "#8fd18f" : "#ddd";
            ctx.textAlign = "right";
            ctx.fillText(`Card ${(card?.timeline_index ?? 0) + 1}/${state.card_count} · ${card?.status ?? "UNKNOWN"}`, this.size[0] - 10, -7);
            ctx.restore();
        };
    },
});
