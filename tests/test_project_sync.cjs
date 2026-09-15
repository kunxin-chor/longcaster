const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function frontend() {
    let extension;
    const queued = [];
    const listeners = new Map();
    const fetches = [];
    const app = {
        registerExtension(value) { extension = value; },
        queuePrompt(...args) { queued.push(args); },
    };
    const api = {
        addEventListener(name, handler) {
            const handlers = listeners.get(name) || [];
            handlers.push(handler);
            listeners.set(name, handlers);
        },
        async fetchApi(path, options) {
            fetches.push({ path, options });
            return { ok: true, async json() { return {}; } };
        },
    };
    const context = vm.createContext({ app, api, setTimeout, clearTimeout });
    const source = fs.readFileSync(path.join(__dirname, "../web/longcaster.js"), "utf8");
    vm.runInContext(source.replace(/^import .*;\r?\n/gm, ""), context);
    return { context, extension, queued, api, listeners, fetches };
}

function projectState(project = "basketball_trial2") {
    return {
        project, generation_mode: "ref2va", refine_cadence: "manual", width: 576, height: 576,
        active_card_id: "active", cards: [
            { id: "accepted", status: "ACCEPTED", seed: 3, assembled_prompt: "old card" },
            { id: "active", status: "DRAFT", seed: 42, requested_duration_seconds: 10, ref_image_size: "max", assembled_prompt: "current card" },
        ],
    };
}

function projectNode() {
    return {
        type: "LongCasterProject",
        widgets: ["project_name", "generation_mode", "refine_cadence", "width", "height", "prompt", "duration_seconds", "seed", "ref_image_size", "action", "command_id"]
            .map((name) => ({ name, value: "stale" })),
        graph: { setDirtyCanvas() {} }, size: [360, 300],
        addWidget() {}, setSize() {},
    };
}

const value = (node, name) => node.widgets.find((item) => item.name === name).value;

test("Project resolution UI requires full invalidation and warns about render loss", () => {
    const source = fs.readFileSync(path.join(__dirname, "../web/longcaster.js"), "utf8");
    assert.match(source, /To change resolution, invalidate all cards first\./);
    assert.match(source, /All renders will be lost\./);
    assert.match(source, /Invalidate All Renders/);
    assert.match(source, /Duplicate and Invalidate Project/);
    assert.match(source, /invalidate_renders: invalidateRenders/);
    assert.match(source, /runAction\("invalidate_all"/);
});

test("different-seed retry always returns a distinct valid integer", () => {
    const { context } = frontend();
    context.crypto = { getRandomValues(values) { values[0] = 42; return values; } };
    assert.equal(context.differentRandomSeed(42), 43);
    const seed = context.differentRandomSeed(7);
    assert.equal(Number.isInteger(seed), true);
    assert.equal(seed >= 0 && seed <= 0xFFFFFFFF, true);
    assert.notEqual(seed, 7);
});

test("prompt diff compares an archived take to the current working prompt", () => {
    const { context } = frontend();
    const diff = Array.from(
        context.promptLineDiff("subject:\nold action\nsound", "subject:\nnew action\nsound\nending"),
        (entry) => ({...entry}),
    );
    assert.deepEqual(diff.map((entry) => entry.kind), ["equal", "delete", "add", "equal", "add"]);
    assert.equal(diff[1].line, "old action");
    assert.equal(diff[2].line, "new action");
    assert.equal(diff[4].line, "ending");
});

test("Draft Take interface includes the captured generation setup", () => {
    const source = fs.readFileSync(path.join(__dirname, "../web/longcaster.js"), "utf8");
    assert.match(source, /Generation Setup/);
    assert.match(source, /Diffusion models/);
    assert.match(source, /LoRAs/);
    assert.match(source, /Patches \/ other MODEL-path nodes/);
    assert.match(source, /take\.generation_setup/);
    assert.match(source, /Take Settings \/ Generation Setup/);
    assert.match(source, /ref_image_size: card\.ref_image_size/);
});

test("loading a Cards project updates node identity, active card, settings, and status together", () => {
    const { context } = frontend();
    const node = projectNode();
    const identity = { type: "LongCasterIdentityAnchor", widgets: [{ name: "project_name", value: "wrong" }] };
    const stitcher = { type: "LongCasterTimelineExport", widgets: [{ name: "project_name", value: "wrong" }] };
    node.graph._nodes = [node, identity, stitcher];
    const state = projectState();
    context.syncProjectNode(node, state);
    assert.equal(value(node, "project_name"), state.project);
    assert.equal(value(node, "generation_mode"), "ref2va");
    assert.equal(value(node, "refine_cadence"), "manual");
    assert.equal(value(node, "width"), 576);
    assert.equal(value(node, "height"), 576);
    assert.equal(value(node, "prompt"), "current card");
    assert.equal(value(node, "duration_seconds"), 10);
    assert.equal(value(node, "seed"), 42);
    assert.equal(value(node, "ref_image_size"), "max");
    assert.equal(node.longcasterState.active_card.id, "active");
    assert.equal(node.longcasterState.card_count, 2);
    assert.match(node.title, /basketball_trial2.*DRAFT/);
    assert.equal(value(identity, "project_name"), "basketball_trial2");
    assert.equal(value(stitcher, "project_name"), "basketball_trial2");
    assert.equal(identity.longcasterProjectState, state);
    assert.match(stitcher.title, /basketball_trial2/);
});

test("Retry waits for project loading and saves before synchronizing and queueing", async () => {
    const { context, queued } = frontend();
    const node = projectNode();
    let loaded;
    node.longcasterCardsWorkspace = {
        loading: new Promise((resolve) => { loaded = resolve; }),
        async flush() { context.syncProjectNode(node, projectState()); return true; },
    };
    const pending = context.queueAction(node, "retry");
    assert.equal(queued.length, 0);
    loaded(true);
    await pending;
    assert.equal(queued.length, 1);
    assert.equal(value(node, "project_name"), "basketball_trial2");
    assert.equal(value(node, "seed"), 42);
    assert.equal(value(node, "action"), "retry");
    assert.notEqual(value(node, "command_id"), "stale");
});

test("failed saves block Retry but do not block Cancel", async () => {
    const { context, queued } = frontend();
    const node = projectNode();
    node.longcasterCardsWorkspace = { async flush() { return false; } };
    await context.queueAction(node, "retry");
    assert.equal(queued.length, 0);
    await context.queueAction(node, "cancel");
    assert.equal(queued.length, 1);
});

test("Stop interrupts ComfyUI and queues the LongCaster cancel action", async () => {
    const { context, queued, fetches } = frontend();
    const node = projectNode();
    const events = [];
    node.longcasterCardsWorkspace = {
        execution: {
            stopping() { events.push("stopping"); },
            append(level, message) { events.push(`${level}:${message}`); },
            begin(action) { events.push(`begin:${action}`); },
            bindPrompt() {},
        },
        async flush() { return true; },
    };
    await context.stopAndUnlock(node);
    assert.equal(fetches[0].path, "/interrupt");
    assert.equal(fetches[0].options.method, "POST");
    assert.equal(value(node, "action"), "cancel");
    assert.equal(queued.length, 1);
    assert.deepEqual(events.slice(0, 3), [
        "stopping",
        "warning:Interrupt acknowledged by ComfyUI. Queueing project unlock.",
        "begin:cancel",
    ]);
});

test("Identity actions use the Project Interface selection and resume the project controller", async () => {
    const { context, queued } = frontend();
    const authority = projectNode();
    const identity = {
        type: "LongCasterIdentityAnchor",
        widgets: [
            { name: "project_name", value: "wrong" },
            { name: "action", value: "inspect" },
            { name: "command_id", value: "old" },
        ],
    };
    const graph = authority.graph;
    identity.graph = graph;
    graph._nodes = [authority, identity];
    graph.longcasterProjectAuthority = authority;
    authority.longcasterCardsWorkspace = {
        state: projectState(), loading: null,
        async flush() { return true; },
    };
    await context.queueAction(identity, "set");
    assert.equal(value(identity, "project_name"), "basketball_trial2");
    assert.equal(value(identity, "action"), "set");
    assert.equal(value(authority, "action"), "resume");
    assert.notEqual(value(authority, "command_id"), "stale");
    assert.equal(queued.length, 1);
});

test("node project edits switch the Cards project and old execution results cannot switch it back", async () => {
    const { extension, context } = frontend();
    class ProjectNode {}
    await extension.beforeRegisterNodeDef(ProjectNode, { name: "LongCasterProject" });
    const node = Object.assign(new ProjectNode(), projectNode());
    node.onNodeCreated();
    const switched = [];
    const refreshed = [];
    node.longcasterCardsWorkspace = {
        switchProject(project) { switched.push(project); },
        refresh(project) { refreshed.push(project); },
        requestedProject: "basketball_trial2",
    };
    context.syncProjectNode(node, projectState());
    const project = node.widgets.find((item) => item.name === "project_name");
    project.callback(project.value);
    assert.deepEqual(switched, ["basketball_trial2"]);
    node.onExecuted({ longcaster_state: [{ project: "old_project", active_card: { prompt: "old prompt" } }] });
    assert.equal(value(node, "prompt"), "current card");
    assert.equal(refreshed.length, 0);
    node.onExecuted({ longcaster_state: [{ project: project.value, active_card: { prompt: "finished", status: "DRAFT" } }] });
    assert.equal(value(node, "prompt"), "finished");
    assert.deepEqual(refreshed, ["basketball_trial2"]);
});

test("identity results refresh only the matching current project", async () => {
    const { extension } = frontend();
    class IdentityNode {}
    await extension.beforeRegisterNodeDef(IdentityNode, { name: "LongCasterIdentityAnchor" });
    const refreshed = [];
    const workspace = {
        state: projectState(), requestedProject: "basketball_trial2",
        refresh(project) { refreshed.push(project); },
    };
    const node = new IdentityNode();
    node.widgets = [{ name: "project_name", value: "basketball_trial2" }, { name: "action", value: "inspect" }];
    node.graph = { _nodes: [{ longcasterCardsWorkspace: workspace }], setDirtyCanvas() {} };
    node.onExecuted({ longcaster_identity: [{ project: "old_project" }] });
    assert.equal(refreshed.length, 0);
    node.onExecuted({ longcaster_identity: [{ project: "basketball_trial2" }] });
    assert.deepEqual(refreshed, ["basketball_trial2"]);
});

test("registered replacement preview refreshes the matching Cards project", async () => {
    const { extension } = frontend();
    class PreviewNode {}
    await extension.beforeRegisterNodeDef(PreviewNode, { name: "LongCasterRegisterPreview" });
    const refreshed = [];
    const authority = projectNode();
    authority.longcasterCardsWorkspace = {
        state: projectState(), requestedProject: "basketball_trial2",
        refresh(project) { refreshed.push(project); },
    };
    const node = new PreviewNode();
    node.graph = authority.graph;
    node.graph.longcasterProjectAuthority = authority;
    node.onExecuted({ longcaster_preview: [{ project: "old_project", card_id: "active" }] });
    assert.equal(refreshed.length, 0);
    node.onExecuted({ longcaster_preview: [{ project: "basketball_trial2", card_id: "active" }] });
    assert.deepEqual(refreshed, ["basketball_trial2"]);
});
