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
        .lc-cards-overlay{position:fixed;inset:0;z-index:10020;background:#08131d;color:#dce8f3;font:13px Inter,system-ui,sans-serif;display:grid;grid-template-rows:auto 1fr auto}
        .lc-cards-overlay[hidden]{display:none}.lc-cards-top{display:flex;align-items:center;gap:12px;padding:10px 14px;background:#0c1c29;border-bottom:1px solid #254052}
        .lc-cards-brand{font-size:20px;font-weight:700;margin-right:8px}.lc-cards-top select,.lc-cards-overlay input,.lc-cards-overlay select,.lc-cards-overlay textarea{background:#0d2131;color:#e7f1f8;border:1px solid #29465b;border-radius:5px;padding:7px;box-sizing:border-box}
        .lc-cards-top button,.lc-cards-overlay button{background:#173149;color:#dce8f3;border:1px solid #31516a;border-radius:5px;padding:7px 11px;cursor:pointer}.lc-cards-overlay button:hover:not(:disabled){background:#1f4565}.lc-cards-overlay button:disabled{opacity:.42;cursor:default}
        .lc-cards-primary{background:#0968bd!important;border-color:#1684e6!important}.lc-cards-accept{background:#088653!important;border-color:#0ebd72!important}.lc-cards-close{margin-left:auto}
        .lc-cards-main{min-height:0;display:grid;grid-template-columns:260px minmax(520px,1fr) 380px}.lc-cards-sidebar,.lc-cards-preview{min-height:0;overflow:auto;background:#0a1824;padding:10px;border-right:1px solid #20394b}.lc-cards-preview{border-right:0;border-left:1px solid #20394b}
        .lc-cards-list{display:grid;gap:5px}.lc-card-item{display:grid!important;grid-template-columns:34px 1fr;gap:7px;text-align:left;padding:9px!important}.lc-card-item.selected{border-color:#168cf0;background:#123b5c}.lc-card-number{font-weight:700}.lc-card-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lc-card-meta{font-size:11px;color:#93a9b9;margin-top:3px}.lc-status-ACCEPTED{color:#35d181}.lc-status-DRAFT{color:#53aaff}.lc-status-FAILED{color:#ff6666}.lc-status-EMPTY{color:#94a4b0}
        .lc-cards-editor{min-width:0;overflow:auto;padding:12px;display:grid;align-content:start;gap:10px}.lc-cards-cardhead{display:flex;align-items:center;gap:10px}.lc-cards-cardhead h2{margin:0}.lc-cards-tabs{display:grid;grid-template-columns:repeat(6,1fr);gap:4px}.lc-cards-tab.active{background:#0968bd;border-color:#1684e6}.lc-cards-section{border:1px solid #223e52;background:#0b1b28;border-radius:7px;padding:12px;display:grid;gap:9px}.lc-cards-sectionbar{display:flex;gap:7px;align-items:center}.lc-cards-sectionbar strong{margin-right:auto}.lc-cards-text{width:100%;min-height:300px;resize:vertical;font:13px ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.55}.lc-cards-provenance{font-size:11px;color:#94a9b8}
        .lc-cards-fields{display:grid;grid-template-columns:repeat(2,minmax(160px,1fr));gap:9px}.lc-cards-field{display:grid;gap:4px}.lc-cards-field label{color:#a9bdca}.lc-cards-prompt{white-space:pre-wrap;max-height:240px;overflow:auto;background:#07131d;padding:10px;border-radius:5px;font:11px ui-monospace,monospace}.lc-legacy{border-color:#a76b1b;background:#2b2112}.lc-legacy pre{white-space:pre-wrap;max-height:280px;overflow:auto}
        .lc-cards-preview h3{margin:4px 0 10px}.lc-cards-video{width:100%;max-height:280px;background:#02070a;border-radius:6px}.lc-cards-info{display:grid;gap:7px;margin-top:12px}.lc-cards-info div{background:#0d2130;border:1px solid #223f53;border-radius:5px;padding:8px;word-break:break-word}.lc-cards-actions{display:flex;gap:8px;margin-left:auto}.lc-cards-footer{display:flex;padding:7px 14px;background:#0c1c29;border-top:1px solid #254052;color:#9bb0bf}.lc-cards-save{margin-left:auto}.lc-cards-error{color:#ff7b7b}.lc-cards-dirty{color:#f5bd4d}@media(max-width:1100px){.lc-cards-main{grid-template-columns:210px 1fr}.lc-cards-preview{display:none}.lc-cards-tabs{grid-template-columns:repeat(3,1fr)}}
    `;
    document.head.append(style);

    const top = cardsElement("div", "lc-cards-top");
    top.append(cardsElement("div", "lc-cards-brand", "LongCaster Cards"));
    const projectSelect = document.createElement("select");
    const refresh = cardsElement("button", "", "Refresh");
    const actions = cardsElement("div", "lc-cards-actions");
    const generate = cardsElement("button", "lc-cards-primary", "Generate Draft");
    const retry = cardsElement("button", "", "Retry Draft");
    const accept = cardsElement("button", "lc-cards-accept", "Accept Draft");
    const append = cardsElement("button", "lc-cards-primary", "Append Card");
    actions.append(generate, retry, accept, append);
    const close = cardsElement("button", "lc-cards-close", "Close");
    top.append(projectSelect, refresh, actions, close);

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
    const legacy = cardsElement("section", "lc-cards-section lc-legacy");
    const legacyMessage = cardsElement("div", "", "This historical card has a flat prompt. It remains unchanged until you explicitly convert it.");
    const legacyPrompt = document.createElement("pre");
    const convertLegacy = cardsElement("button", "", "Convert into Detailed Description");
    legacy.append(legacyMessage, legacyPrompt, convertLegacy);
    const fields = cardsElement("div", "lc-cards-fields");
    const durationField = cardsElement("div", "lc-cards-field");
    const duration = document.createElement("input"); duration.type = "number"; duration.min = "0.1"; duration.max = "120"; duration.step = "0.1";
    durationField.append(cardsElement("label", "", "Duration (seconds)"), duration);
    const seedField = cardsElement("div", "lc-cards-field");
    const seed = document.createElement("input"); seed.type = "text"; seed.inputMode = "numeric";
    seedField.append(cardsElement("label", "", "Seed"), seed);
    const modeField = cardsElement("div", "lc-cards-field");
    const mode = document.createElement("input"); mode.readOnly = true;
    modeField.append(cardsElement("label", "", "Generation mode"), mode);
    const strategyField = cardsElement("div", "lc-cards-field");
    const strategy = document.createElement("input"); strategy.readOnly = true;
    strategyField.append(cardsElement("label", "", "Continuation strategy"), strategy);
    fields.append(durationField, seedField, modeField, strategyField);
    const details = document.createElement("details");
    const summary = document.createElement("summary"); summary.textContent = "Assembled Prompt Preview";
    const promptPreview = cardsElement("pre", "lc-cards-prompt");
    details.append(summary, promptPreview);
    editor.append(cardHead, tabs, section, legacy, fields, details);

    const preview = cardsElement("aside", "lc-cards-preview");
    preview.append(cardsElement("h3", "", "Card Preview"));
    const video = cardsElement("video", "lc-cards-video"); video.controls = true; video.preload = "metadata";
    const info = cardsElement("div", "lc-cards-info");
    preview.append(video, info);
    main.append(sidebar, editor, preview);
    const footer = cardsElement("div", "lc-cards-footer");
    const message = cardsElement("span", "", "Loading…");
    const saveState = cardsElement("span", "lc-cards-save");
    footer.append(message, saveState);
    root.append(top, main, footer);
    document.body.append(root);

    const workspace = {
        root, node, state: null, selectedCardId: null, selectedSection: CARD_SECTIONS[0][0],
        dirty: false, dirtySections: new Set(), timer: null, saving: null, editVersion: 0,
        followActive: false,
    };
    node.longcasterCardsWorkspace = workspace;

    const selectedCard = () => workspace.state?.cards?.find((card) => card.id === workspace.selectedCardId);
    const editable = (card) => Boolean(card && card.id === workspace.state?.active_card_id && ["EMPTY", "DRAFT", "FAILED"].includes(card.status));
    const setMessage = (value, error = false) => { message.textContent = value; message.className = error ? "lc-cards-error" : ""; };
    const renderSaveState = () => {
        saveState.textContent = workspace.saving ? "Saving…" : workspace.dirty ? "Unsaved changes" : `Saved · revision ${workspace.state?.revision ?? "?"}`;
        saveState.className = `lc-cards-save${workspace.dirty ? " lc-cards-dirty" : ""}`;
    };

    function updateState(state, keepSelection = true) {
        workspace.state = state;
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
            button.onclick = () => { workspace.selectedSection = name; render(); textarea.focus(); };
            tabs.append(button);
        }
        const structured = card.prompt_format === "structured_v1";
        section.hidden = !structured;
        legacy.hidden = structured;
        if (structured) {
            const record = card.prompt_sections[workspace.selectedSection];
            sectionName.textContent = CARD_SECTIONS.find(([name]) => name === workspace.selectedSection)?.[1] || workspace.selectedSection;
            textarea.value = record.text;
            textarea.readOnly = !isEditable;
            const source = record.provenance.source_card_id ? ` · source ${record.provenance.source_card_id}` : "";
            provenance.textContent = `${record.provenance.source_type}${source}${record.provenance.modified_after_copy ? " · modified after copy" : ""}`;
        } else {
            legacyPrompt.textContent = card.assembled_prompt;
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
        duration.value = card.requested_duration_seconds ?? 5;
        seed.value = String(card.seed ?? 0);
        duration.disabled = !isEditable; seed.disabled = !isEditable;
        mode.value = state.generation_mode; strategy.value = card.continuation_strategy || "unknown";
        promptPreview.textContent = card.assembled_prompt || "";
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
        const referenceCount = card.reference_set?.resources?.length ?? 0;
        const referenceItems = (card.reference_set?.resources || []).map((item) => `${item.kind}:${String(item.id || "?").slice(0, 8)}`).join(", ");
        const referenceLabel = referenceCount
            ? `${card.reference_set.packet_name || card.reference_set.packet_id || "graph packet"} · ${referenceCount} resource(s) · ${referenceItems}`
            : "not yet recorded / graph-provided";
        info.replaceChildren(
            cardsElement("div", "", `Timeline predecessor: ${card.timeline_predecessor_id || "none"}`),
            cardsElement("div", "", `Generation parent: ${card.generation_parent_id || "none"}`),
            cardsElement("div", "", `Accepted take: ${card.accepted_publication_id || "none"}`),
            cardsElement("div", "", `Current-state anchor: ${card.current_state_anchor_id || "none"}`),
            cardsElement("div", "", `Active identity anchor: ${state.active_identity_anchor?.anchor_id || "none"}`),
            cardsElement("div", "", `Reference set: ${referenceLabel}`),
            cardsElement("div", "", `Prompt SHA-256: ${card.prompt_hash || "none"}`),
            ...(card.draft_inputs_dirty ? [cardsElement("div", "lc-cards-dirty", "Draft inputs changed · Retry Draft before accepting")]: []),
            ...(card.last_error ? [cardsElement("div", "lc-cards-error", `Last error: ${card.last_error}`)] : []),
        );
        renderSaveState();
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
        try {
            const body = await responseJson(await api.fetchApi(`/longcaster/cards/state?project=${encodeURIComponent(project || "")}`));
            updateState(body, !workspace.followActive); workspace.followActive = false;
            projectSelect.value = body.project; setMessage(body.message || "Project loaded.");
        } catch (error) { setMessage(`Load failed: ${error.message}`, true); }
    }
    workspace.load = load;

    async function loadProjects() {
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/projects"));
            const current = widget(node, "project_name")?.value;
            projectSelect.replaceChildren();
            for (const project of body.projects || []) {
                const option = document.createElement("option"); option.value = project.project; option.textContent = `${project.project} (${project.card_count})`; projectSelect.append(option);
            }
            const selected = body.projects?.some((item) => item.project === current) ? current : body.projects?.[0]?.project;
            if (selected) { projectSelect.value = selected; await load(selected); }
            else setMessage("No LongCaster projects found.", true);
        } catch (error) { setMessage(`Project refresh failed: ${error.message}`, true); }
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

    textarea.oninput = () => { const card = selectedCard(); card.prompt_sections[workspace.selectedSection].text = textarea.value; markDirty(workspace.selectedSection); };
    duration.oninput = () => { selectedCard().requested_duration_seconds = Number(duration.value); markDirty(); };
    seed.oninput = () => { selectedCard().seed = Number(seed.value); markDirty(); };
    copyPrevious.onclick = () => copyFrom(selectedCard().timeline_predecessor_id);
    copySelected.onclick = () => copyFrom(sourceSelect.value);
    clearSection.onclick = () => clearCurrentSection();
    convertLegacy.onclick = async () => {
        const card = selectedCard();
        if (!confirm("Convert this flat prompt into detailed_description? The original text will be preserved inside that section.")) return;
        try {
            const body = await responseJson(await api.fetchApi("/longcaster/cards/card", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({project: workspace.state.project, card_id: card.id, revision: workspace.state.revision, convert_legacy: true, sections: {detailed_description: card.assembled_prompt}}),
            }));
            workspace.selectedSection = "detailed_description"; updateState(body); setMessage("Legacy prompt converted.");
        } catch (error) { setMessage(`Conversion failed: ${error.message}`, true); }
    };
    previousCard.onclick = async () => { const card = selectedCard(); const target = workspace.state.cards[card.timeline_index - 1]; if (target && await flush()) { workspace.selectedCardId = target.id; render(); } };
    nextCard.onclick = async () => { const card = selectedCard(); const target = workspace.state.cards[card.timeline_index + 1]; if (target && await flush()) { workspace.selectedCardId = target.id; render(); } };
    generate.onclick = () => runAction("generate"); retry.onclick = () => runAction("retry"); accept.onclick = () => runAction("accept"); append.onclick = () => runAction("append");
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
