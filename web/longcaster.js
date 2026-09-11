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

app.registerExtension({
    name: "minimax.h3.longcaster",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "LongCasterProject") return;

        const originalCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalCreated?.apply(this, arguments);
            this.addWidget("button", "Resume Project", null, () => queueAction(this, "resume"));
            this.addWidget("button", "Stop Render / Unlock", null, () => stopAndUnlock(this));
            this.addWidget("button", "Generate Draft", null, () => queueAction(this, "generate"));
            this.addWidget("button", "Retry Draft", null, () => queueAction(this, "retry"));
            this.addWidget("button", "Accept Draft", null, () => queueAction(this, "accept"));
            this.addWidget("button", "Append Card", null, () => queueAction(this, "append"));
            const computed = this.computeSize?.();
            this.setSize([
                Math.max(this.size[0], 360),
                Math.max(this.size[1], computed?.[1] ?? this.size[1]),
            ]);
            return result;
        };

        const originalExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            originalExecuted?.apply(this, arguments);
            const state = message?.longcaster_state?.[0];
            if (!state) return;
            this.longcasterState = state;
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
            ctx.fillText(
                `Card ${(card?.timeline_index ?? 0) + 1}/${state.card_count} · ${card?.status ?? "UNKNOWN"}`,
                this.size[0] - 10,
                -7,
            );
            ctx.restore();
        };
    },
});
