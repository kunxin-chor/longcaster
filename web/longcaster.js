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
