import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function widget(node, name) {
    return node.widgets?.find((item) => item.name === name);
}

function commandId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function queueAction(node, action) {
    const actionWidget = widget(node, "action");
    const commandWidget = widget(node, "command_id");
    if (actionWidget) actionWidget.value = action;
    if (commandWidget) commandWidget.value = commandId();
    node.graph?.setDirtyCanvas(true, true);
    app.queuePrompt(0, 1);
}

async function stopAndUnlock(node) {
    try {
        await api.fetchApi("/interrupt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
    } finally {
        queueAction(node, "cancel");
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
    try {
        const body = await responseJson(await api.fetchApi("/longcaster/identity/projects"));
        const select = node.longcasterIdentityProjectSelect;
        const current = String(widget(node, "project_name")?.value ?? "");
        select.replaceChildren();
        for (const project of body.projects || []) {
            const option = document.createElement("option");
            option.value = project.project;
            option.textContent = project.project;
            select.append(option);
        }
        const state = body.projects?.find((item) => item.project === current) || body.projects?.[0];
        if (state) {
            select.value = state.project;
            const projectWidget = widget(node, "project_name");
            if (projectWidget) projectWidget.value = state.project;
            updateIdentityPanel(node, state);
        } else {
            node.longcasterIdentityStatus.textContent = "No LongCaster projects found.";
        }
    } catch (error) {
        node.longcasterIdentityStatus.textContent = `Refresh failed: ${error.message}`;
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
    projectSelect.onchange = async () => {
        const projectWidget = widget(node, "project_name");
        if (projectWidget) projectWidget.value = projectSelect.value;
        try {
            const query = `project=${encodeURIComponent(projectSelect.value)}&subject_id=${encodeURIComponent(widget(node, "subject_id")?.value || "<Subject 1>")}`;
            updateIdentityPanel(node, await responseJson(await api.fetchApi(`/longcaster/identity/state?${query}`)));
        } catch (error) {
            status.textContent = `Project load failed: ${error.message}`;
        }
    };
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

function createCardsWorkspace(node) {
    if (node.longcasterCardsWorkspace) {
        node.longcasterCardsWorkspace.root.hidden = false;
        node.longcasterCardsWorkspace.load();
        return;
    }

    const root = cardsElement("div", "lc-cards-overlay");
    const style = document.createElement("style");
    style.textContent = `
        .lc-cards-overlay{position:fixed;inset:0;z-index:10020;background:#08131d;color:#dce8f3;font:13px Inter,system-ui,sans-serif;display:grid;grid-template-rows:auto auto 1fr auto}
        .lc-cards-overlay[hidden],.lc-cards-overlay [hidden]{display:none!important}.lc-cards-top{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#0c1c29;border-bottom:1px solid #254052}
        .lc-cards-brand{font-size:20px;font-weight:700;margin-right:8px}.lc-cards-top select,.lc-cards-overlay input,.lc-cards-overlay select,.lc-cards-overlay textarea{background:#0d2131;color:#e7f1f8;border:1px solid #29465b;border-radius:5px;padding:7px;box-sizing:border-box}
        .lc-cards-top button,.lc-cards-overlay button{background:#173149;color:#dce8f3;border:1px solid #31516a;border-radius:5px;padding:7px 11px;cursor:pointer}.lc-cards-overlay button:hover:not(:disabled){background:#1f4565}.lc-cards-overlay button:disabled{opacity:.42;cursor:default}
        .lc-cards-primary{background:#0968bd!important;border-color:#1684e6!important}.lc-cards-accept{background:#088653!important;border-color:#0ebd72!important}.lc-cards-close{margin-left:auto}
        .lc-cards-projectpath{color:#89a2b4;font:11px ui-monospace,monospace}.lc-cards-new{display:grid;grid-template-columns:2fr repeat(6,minmax(90px,1fr)) auto auto;gap:8px;align-items:end;padding:10px 14px;background:#102333;border-bottom:1px solid #29465b}.lc-cards-new[hidden]{display:none}.lc-cards-new label{display:grid;gap:4px;color:#a9bdca}.lc-cards-resolution{min-height:31px;display:flex;align-items:center;color:#dce8f3;font:12px ui-monospace,monospace;white-space:nowrap}
        .lc-cards-main{min-height:0;display:grid;grid-template-columns:260px minmax(620px,1fr) 420px}.lc-cards-sidebar,.lc-cards-preview{min-height:0;overflow:auto;background:#0a1824;padding:10px;border-right:1px solid #20394b}.lc-cards-preview{border-right:0;border-left:1px solid #20394b}
        .lc-cards-list{display:grid;gap:5px}.lc-card-item{display:grid!important;grid-template-columns:34px 1fr;gap:7px;text-align:left;padding:9px!important}.lc-card-item.selected{border-color:#168cf0;background:#123b5c}.lc-card-number{font-weight:700}.lc-card-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lc-card-meta{font-size:11px;color:#93a9b9;margin-top:3px}.lc-status-ACCEPTED{color:#35d181}.lc-status-DRAFT{color:#53aaff}.lc-status-FAILED{color:#ff6666}.lc-status-EMPTY{color:#94a4b0}
        .lc-cards-editor{min-width:0;overflow:auto;padding:12px;display:grid;align-content:start;gap:10px}.lc-cards-cardhead{display:flex;align-items:center;gap:10px}.lc-cards-cardhead h2{margin:0}.lc-cards-editing{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.lc-cards-fullpanel,.lc-cards-sectioncolumn{min-width:0;display:grid;align-content:start;gap:7px}.lc-cards-fullbar{display:flex;align-items:center;gap:8px}.lc-cards-fullbar h3{margin:0 auto 0 0}.lc-cards-full{box-sizing:border-box;white-space:pre-wrap;overflow:auto;height:430px;margin:0;background:#07131d;border:1px solid #223e52;border-radius:7px;padding:12px;font:12px ui-monospace,monospace;line-height:1.55}.lc-cards-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}.lc-cards-tab.active{background:#0968bd;border-color:#1684e6}.lc-cards-section{border:1px solid #223e52;background:#0b1b28;border-radius:7px;padding:12px;display:grid;gap:9px}.lc-cards-sectionbar{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.lc-cards-sectionbar strong{margin-right:auto}.lc-cards-text{width:100%;height:330px;resize:vertical;font:13px ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.55}.lc-cards-import .lc-cards-text{height:250px}.lc-cards-provenance{font-size:11px;color:#94a9b8}
        .lc-cards-fields{display:grid;grid-template-columns:repeat(2,minmax(160px,1fr));gap:9px}.lc-cards-field{display:grid;gap:4px}.lc-cards-field label{color:#a9bdca}.lc-cards-prompt{white-space:pre-wrap;max-height:240px;overflow:auto;background:#07131d;padding:10px;border-radius:5px;font:11px ui-monospace,monospace}.lc-legacy{border-color:#a76b1b;background:#2b2112}.lc-legacy pre{white-space:pre-wrap;max-height:280px;overflow:auto}
        .lc-cards-preview h3{margin:12px 0 8px}.lc-cards-preview h3:first-child{margin-top:4px}.lc-cards-video{width:100%;max-height:280px;background:#02070a;border-radius:6px}.lc-cards-info,.lc-cards-assets{display:grid;gap:7px;margin-top:10px}.lc-cards-info>div,.lc-cards-asset{background:#0d2130;border:1px solid #223f53;border-radius:5px;padding:8px;word-break:break-word}.lc-cards-asset{display:grid;grid-template-columns:48px 1fr auto;gap:8px;align-items:center}.lc-cards-asset img{width:48px;height:48px;object-fit:cover;border-radius:4px;background:#02070a}.lc-cards-asset small{color:#91aabd}.lc-cards-help{color:#91aabd;font-size:11px;line-height:1.4}.lc-cards-identity-create{display:grid;grid-template-columns:1fr 90px;gap:6px}.lc-cards-identity-create .wide{grid-column:1/-1}.lc-cards-actions{display:flex;gap:8px;margin-left:auto}.lc-cards-footer{display:flex;padding:7px 14px;background:#0c1c29;border-top:1px solid #254052;color:#9bb0bf}.lc-cards-save{margin-left:auto}.lc-cards-error{color:#ff7b7b}.lc-cards-dirty{color:#f5bd4d}@media(max-width:1250px){.lc-cards-main{grid-template-columns:220px 1fr}.lc-cards-preview{display:none}.lc-cards-new{grid-template-columns:repeat(4,1fr)}.lc-cards-editing{grid-template-columns:1fr}}
    `;
    document.head.append(style);

    const top = cardsElement("div", "lc-cards-top");
    top.append(cardsElement("div", "lc-cards-brand", "LongCaster Studio"));
    const projectSelect = document.createElement("select");
    const refresh = cardsElement("button", "", "Refresh");
    const newProject = cardsElement("button", "", "New Project");
    const projectPath = cardsElement("span", "lc-cards-projectpath");
    const actions = cardsElement("div", "lc-cards-actions");
    const generate = cardsElement("button", "lc-cards-primary", "Generate Draft");
    const retry = cardsElement("button", "", "Retry Draft");
    const accept = cardsElement("button", "lc-cards-accept", "Accept Draft");
    const append = cardsElement("button", "lc-cards-primary", "Append Card");
    actions.append(generate, retry, accept, append);
    const close = cardsElement("button", "lc-cards-close", "Close");
    top.append(projectSelect, refresh, newProject, projectPath, actions, close);

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
    cardHead.append(previousCard, cardTitle, nextCard, cardStatus);
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
    for (const [value, label] of [["direct_mmh3", "Continue previous card (direct MMH3)"], ["independent", "Independent shot"]]) { const option = document.createElement("option"); option.value = value; option.textContent = label; strategy.append(option); }
    strategyField.append(cardsElement("label", "", "Continuation strategy"), strategy);
    strategyField.append(cardsElement("div", "lc-cards-help", "Direct MMH3 preserves the accepted parent’s final 39 frames and joint audio/video latent. Independent starts without a generation parent."));
    fields.append(durationField, seedField, modeField, strategyField);
    const editing = cardsElement("div", "lc-cards-editing");
    const fullPanel = cardsElement("div", "lc-cards-fullpanel");
    const fullBar = cardsElement("div", "lc-cards-fullbar");
    const importFull = cardsElement("button", "", "Paste Full Prompt");
    fullBar.append(cardsElement("h3", "", "Full Prompt"), importFull);
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
    editing.append(fullPanel, sectionColumn);
    editor.append(cardHead, editing, legacy, fields);

    const preview = cardsElement("aside", "lc-cards-preview");
    preview.append(cardsElement("h3", "", "Card Preview"));
    const video = cardsElement("video", "lc-cards-video"); video.controls = true; video.preload = "metadata";
    const buildPreview = cardsElement("button", "", "Generate missing preview");
    const info = cardsElement("div", "lc-cards-info");
    const identities = cardsElement("div", "lc-cards-assets");
    const references = cardsElement("div", "lc-cards-assets");
    const editReferences = cardsElement("button", "", "Edit connected reference graph");
    const identityHeading = cardsElement("div", "lc-cards-fullbar");
    const refreshIdentities = cardsElement("button", "", "Refresh");
    identityHeading.append(cardsElement("h3", "", "Identity Checkpoints"), refreshIdentities);
    const referenceHeading = cardsElement("div", "lc-cards-fullbar");
    const refreshReferences = cardsElement("button", "", "Refresh");
    referenceHeading.append(cardsElement("h3", "", "Reference Resources"), refreshReferences);
    const identityCreate = cardsElement("div", "lc-cards-identity-create");
    const identitySubject = document.createElement("input"); identitySubject.value = "<Subject 1>"; identitySubject.placeholder = "<Subject 1>";
    const identityFrame = document.createElement("input"); identityFrame.type = "number"; identityFrame.min = "0"; identityFrame.value = "0";
    const identityLabel = document.createElement("input"); identityLabel.value = "identity checkpoint"; identityLabel.placeholder = "Identity label"; identityLabel.className = "wide";
    const identityScope = document.createElement("select"); identityScope.className = "wide";
    for (const value of ["face_only", "face_clothing", "face_body", "everything", "custom"]) { const option = document.createElement("option"); option.value = value; option.textContent = value.replaceAll("_", " "); identityScope.append(option); }
    const identityCustom = document.createElement("textarea"); identityCustom.className = "wide"; identityCustom.placeholder = "For custom scope, describe exactly what this image may contribute."; identityCustom.hidden = true;
    const createIdentity = cardsElement("button", "wide", "Use selected card frame as identity");
    identityCreate.append(identitySubject, identityFrame, identityLabel, identityScope, identityCustom, createIdentity);
    preview.append(video, buildPreview, info, identityHeading, identities, identityCreate, referenceHeading, references, editReferences);
    main.append(sidebar, editor, preview);
    const footer = cardsElement("div", "lc-cards-footer");
    const message = cardsElement("span", "", "Loading…");
    const saveState = cardsElement("span", "lc-cards-save");
    footer.append(message, saveState);
    root.append(top, newPanel, main, footer);
    document.body.append(root);

    const workspace = {
        root, node, state: null, selectedCardId: null, selectedSection: CARD_SECTIONS[0][0],
        dirty: false, dirtySections: new Set(), timer: null, saving: null, editVersion: 0,
        followActive: false, loadEpoch: 0, projectListEpoch: 0, importCardId: null,
    };
    node.longcasterCardsWorkspace = workspace;

    const selectedCard = () => workspace.state?.cards?.find((card) => card.id === workspace.selectedCardId);
    const editable = (card) => Boolean(card && card.id === workspace.state?.active_card_id && ["EMPTY", "DRAFT", "FAILED"].includes(card.status));
    const setMessage = (value, error = false) => { message.textContent = value; message.className = error ? "lc-cards-error" : ""; };
    const shortId = (value) => value ? String(value).slice(0, 8) : "none";
    const cardLabel = (cardId) => {
        const item = workspace.state?.cards?.find((card) => card.id === cardId);
        return item ? `Card ${item.timeline_index + 1} · ${item.title || "Untitled"} · ${shortId(item.id)}` : "none";
    };
    const syncProjectNode = (state) => {
        for (const [name, value] of [["project_name", state.project], ["generation_mode", state.generation_mode], ["width", state.width], ["height", state.height]]) {
            const item = widget(node, name);
            if (item) item.value = value;
        }
        node.graph?.setDirtyCanvas(true, true);
    };
    const renderSaveState = () => {
        saveState.textContent = workspace.saving ? "Saving…" : workspace.dirty ? "Unsaved changes" : `Saved · revision ${workspace.state?.revision ?? "?"}`;
        saveState.className = `lc-cards-save${workspace.dirty ? " lc-cards-dirty" : ""}`;
    };

    function updateState(state, keepSelection = true) {
        workspace.state = state;
        syncProjectNode(state);
        projectPath.textContent = state.project_folder || "";
        const exists = state.cards?.some((card) => card.id === workspace.selectedCardId);
        if (!keepSelection || !exists) workspace.selectedCardId = state.active_card_id || state.cards?.[0]?.id;
        render();
    }

    function render() {
        const state = workspace.state;
        if (!state) return;
        const card = selectedCard();
        list.replaceChildren();
        for (const item of state.cards || []) {
            const button = cardsElement("button", `lc-card-item${item.id === card?.id ? " selected" : ""}`);
            const number = cardsElement("div", "lc-card-number", String(item.timeline_index + 1).padStart(2, "0"));
            const body = cardsElement("div", "");
            body.append(cardsElement("div", "lc-card-title", item.title || "Untitled card"));
            body.append(cardsElement("div", `lc-card-meta lc-status-${item.status}`, `${item.status} · ${Number(item.actual_duration_seconds ?? item.requested_duration_seconds ?? 0).toFixed(1)}s`));
            button.append(number, body);
            button.onclick = async () => { if (await flush()) { workspace.selectedCardId = item.id; render(); } };
            list.append(button);
        }
        if (!card) return;
        const isEditable = editable(card);
        cardTitle.textContent = `Card ${card.timeline_index + 1}`;
        cardStatus.textContent = `${card.status} · ${card.id}`;
        cardStatus.className = `lc-status-${card.status}`;
        previousCard.disabled = card.timeline_index < 1;
        nextCard.disabled = card.timeline_index >= state.cards.length - 1;
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
        mode.value = state.generation_mode; mode.disabled = !state.generation_mode_editable;
        mode.title = state.generation_mode_editable ? "Mode can change until the first render." : "Mode is fixed because this project already has rendered media.";
        strategy.value = card.continuation_strategy || "independent";
        strategy.disabled = !isEditable || !card.timeline_predecessor_id;
        generate.disabled = card.id !== state.active_card_id || !["EMPTY", "FAILED"].includes(card.status) || Boolean(state.pending_operation);
        retry.disabled = card.id !== state.active_card_id || card.status !== "DRAFT" || Boolean(state.pending_operation);
        accept.disabled = card.id !== state.active_card_id || card.status !== "DRAFT" || card.draft_inputs_dirty || Boolean(state.pending_operation);
        append.disabled = card.id !== state.active_card_id || card.status !== "ACCEPTED" || Boolean(state.pending_operation);
        if (card.preview_available) {
            const nextSource = api.apiURL(`/longcaster/cards/preview?project=${encodeURIComponent(state.project)}&card=${encodeURIComponent(card.id)}`);
            if (video.dataset.source !== nextSource) { video.dataset.source = nextSource; video.src = nextSource; video.load(); }
        } else if (video.dataset.source) {
            video.pause(); video.removeAttribute("src"); video.dataset.source = ""; video.load();
        }
        buildPreview.hidden = card.preview_available || card.status !== "ACCEPTED";
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
        renderIdentities(card);
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

    function renderIdentities(card) {
        identities.replaceChildren();
        const items = workspace.state.identity_anchors || [];
        if (!items.length) identities.append(cardsElement("div", "lc-cards-help", "No saved identity checkpoints. Choose an accepted card and frame below to create one through the workflow's Identity Anchor node."));
        for (const item of items) {
            const row = cardsElement("div", "lc-cards-asset");
            const image = document.createElement("img");
            if (item.asset_available) image.src = api.apiURL(`/longcaster/cards/anchor?project=${encodeURIComponent(workspace.state.project)}&anchor=${encodeURIComponent(item.anchor_id)}`);
            const body = document.createElement("div");
            body.append(cardsElement("div", "", `${item.label}${item.active_for?.length ? " · ACTIVE" : ""}`));
            body.append(cardsElement("small", "", `${item.subject_id} · Card ${item.source_card_number}, frame ${item.source_preview_frame_index ?? "?"} · ${item.identity_scope.replaceAll("_", " ")} · ${shortId(item.anchor_id)}`));
            const controls = document.createElement("div");
            const use = cardsElement("button", "", item.active_for?.length ? (item.enabled ? "Disable" : "Enable") : "Use");
            use.onclick = () => manageIdentity(item.active_for?.length ? (item.enabled ? "disable" : "enable") : "bind", item);
            controls.append(use);
            if (item.active_for?.length) {
                const clear = cardsElement("button", "", "Clear");
                clear.onclick = () => manageIdentity("clear", item);
                controls.append(clear);
            }
            row.append(image, body, controls); identities.append(row);
        }
        identityCreate.hidden = card.status !== "ACCEPTED";
        identityFrame.max = String(Math.max(0, Math.round(Number(card.actual_duration_seconds || 0) * 24) - 1));
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
        return {packetName, resources};
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
            continuation_strategy: card.continuation_strategy,
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

    async function flush() {
        clearTimeout(workspace.timer);
        if (workspace.saving && !await workspace.saving) return false;
        if (workspace.dirty) return save();
        return true;
    }

    async function load(project = widget(node, "project_name")?.value) {
        const loadEpoch = ++workspace.loadEpoch;
        try {
            const body = await responseJson(await api.fetchApi(`/longcaster/cards/state?project=${encodeURIComponent(project || "")}`));
            if (loadEpoch !== workspace.loadEpoch) return false;
            updateState(body, !workspace.followActive); workspace.followActive = false;
            projectSelect.value = body.project; setMessage(body.message || "Project loaded.");
            return true;
        } catch (error) {
            if (loadEpoch === workspace.loadEpoch) setMessage(`Load failed: ${error.message}`, true);
            return false;
        }
    }
    workspace.load = load;

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
        workspace.projectListEpoch += 1;
        try {
            const resolution = selectedNewResolution();
            const body = await responseJson(await api.fetchApi("/longcaster/cards/projects", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    project: newName.value.trim(), generation_mode: newMode.value,
                    width: resolution.width, height: resolution.height,
                    duration_seconds: Number(newDuration.value), seed: Number(newSeed.value),
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

    function identityNode() {
        return node.graph?._nodes?.find((item) => item.comfyClass === "LongCasterIdentityAnchor" || item.type === "LongCasterIdentityAnchor");
    }

    function queueIdentity(action, card) {
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

    async function runAction(action) {
        if (!await flush()) return;
        const card = selectedCard();
        if (action === "accept" && card.draft_inputs_dirty) {
            setMessage("Draft inputs changed. Retry Draft before accepting.", true);
            render();
            return;
        }
        const projectWidget = widget(node, "project_name");
        const promptWidget = widget(node, "prompt");
        const durationWidget = widget(node, "duration_seconds");
        const seedWidget = widget(node, "seed");
        if (projectWidget) projectWidget.value = workspace.state.project;
        if (promptWidget) promptWidget.value = card.assembled_prompt;
        if (durationWidget) durationWidget.value = Number(card.requested_duration_seconds);
        if (seedWidget) seedWidget.value = Number(card.seed);
        workspace.followActive = action === "append";
        setMessage(`${action} queued…`); queueAction(node, action);
    }

    textarea.oninput = () => {
        const card = selectedCard();
        card.prompt_sections[workspace.selectedSection].text = textarea.value;
        card.assembled_prompt = CARD_SECTIONS.map(([name]) => `${name}:${card.prompt_sections[name].text ? `\n${card.prompt_sections[name].text}` : ""}`).join("\n\n");
        fullPrompt.textContent = card.assembled_prompt;
        markDirty(workspace.selectedSection);
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
    strategy.onchange = () => { selectedCard().continuation_strategy = strategy.value; markDirty(); };
    mode.onchange = () => saveProjectMode();
    identityScope.onchange = () => { identityCustom.hidden = identityScope.value !== "custom"; };
    const syncPreviewFrame = () => { if (video.src) identityFrame.value = String(Math.max(0, Math.round(video.currentTime * 24))); };
    video.addEventListener("pause", syncPreviewFrame);
    video.addEventListener("seeked", syncPreviewFrame);
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
    previousCard.onclick = async () => { const card = selectedCard(); const target = workspace.state.cards[card.timeline_index - 1]; if (target && await flush()) { workspace.selectedCardId = target.id; render(); } };
    nextCard.onclick = async () => { const card = selectedCard(); const target = workspace.state.cards[card.timeline_index + 1]; if (target && await flush()) { workspace.selectedCardId = target.id; render(); } };
    generate.onclick = () => runAction("generate"); retry.onclick = () => runAction("retry"); accept.onclick = () => runAction("accept"); append.onclick = () => runAction("append");
    newProject.onclick = () => { newPanel.hidden = !newPanel.hidden; if (!newPanel.hidden) newName.focus(); };
    cancelCreate.onclick = () => { newPanel.hidden = true; };
    createProject.onclick = () => createNewProject();
    buildPreview.onclick = () => queueIdentity("build_preview", selectedCard());
    createIdentity.onclick = () => queueIdentity("set", selectedCard());
    refreshIdentities.onclick = async () => { if (await flush()) await load(workspace.state.project); };
    refreshReferences.onclick = async () => { if (await flush()) { await load(workspace.state.project); renderReferences(selectedCard()); } };
    editReferences.onclick = () => focusReferenceGraph();
    projectSelect.onchange = async () => { if (!await flush()) return; const projectWidget = widget(node, "project_name"); if (projectWidget) projectWidget.value = projectSelect.value; workspace.selectedCardId = null; await load(projectSelect.value); };
    refresh.onclick = () => loadProjects(); close.onclick = async () => { if (await flush()) root.hidden = true; };
    window.addEventListener("beforeunload", (event) => { if (workspace.dirty) { event.preventDefault(); event.returnValue = ""; } });
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
                const action = widget(this, "action");
                if (action) action.value = "inspect";
                updateIdentityPanel(this, state);
                for (const projectNode of this.graph?._nodes || []) {
                    if ((projectNode.comfyClass === "LongCasterProject" || projectNode.type === "LongCasterProject") && projectNode.longcasterCardsWorkspace) {
                        projectNode.longcasterCardsWorkspace.load(state.project);
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
        if (nodeData.name !== "LongCasterProject") return;

        const originalCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalCreated?.apply(this, arguments);
            const action = widget(this, "action");
            if (action) action.value = "resume";
            this.addWidget("button", "Resume Project", null, () => queueAction(this, "resume"));
            this.addWidget("button", "Stop Render / Unlock", null, () => stopAndUnlock(this));
            this.addWidget("button", "Generate Draft", null, () => queueAction(this, "generate"));
            this.addWidget("button", "Retry Draft", null, () => queueAction(this, "retry"));
            this.addWidget("button", "Accept Draft", null, () => queueAction(this, "accept"));
            this.addWidget("button", "Unpublish Latest Card", null, () => queueAction(this, "unpublish"));
            this.addWidget("button", "Append Card", null, () => queueAction(this, "append"));
            this.addWidget("button", "Open Cards Interface", null, () => createCardsWorkspace(this));
            const computed = this.computeSize?.();
            this.setSize([Math.max(this.size[0], 360), Math.max(this.size[1], computed?.[1] ?? this.size[1])]);
            return result;
        };

        const originalExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            originalExecuted?.apply(this, arguments);
            const state = message?.longcaster_state?.[0];
            if (!state) return;
            this.longcasterState = state;
            const action = widget(this, "action");
            if (action) action.value = "resume";
            const card = state.active_card;
            if (card) {
                const prompt = widget(this, "prompt");
                const duration = widget(this, "duration_seconds");
                const seed = widget(this, "seed");
                if (prompt) prompt.value = card.prompt ?? prompt.value;
                if (duration) duration.value = card.requested_duration_seconds ?? duration.value;
                if (seed) seed.value = card.seed ?? seed.value;
            }
            this.title = `MiniMax H3 LongCaster · ${card?.status ?? "UNKNOWN"}`;
            if (this.longcasterCardsWorkspace && !this.longcasterCardsWorkspace.root.hidden) {
                this.longcasterCardsWorkspace.load(state.project);
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
