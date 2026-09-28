import assert from "node:assert/strict";
import { test } from "node:test";
import { hiresCardState, repairHiresWidgetDefaults } from "../web/hires.js";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const first = {id: "first", status: "ACCEPTED", timeline_index: 0, continuation_strategy: "independent"};
const child = {id: "child", status: "ACCEPTED", timeline_index: 1, continuation_strategy: "direct_mmh3", generation_parent_id: "first"};
const project = {width: 544, height: 960, hires_cadence: "manual", cards: [first, child], active_card_id: "child"};

test("accepted historical cards can upscale without changing the active card", () => {
    const view = hiresCardState(first, project);
    assert.equal(view.canCreate, true);
    assert.deepEqual([view.width, view.height], [1088, 1920]);
    assert.equal(project.active_card_id, "child");
});

test("direct continuation requires a ready parent HR", () => {
    const view = hiresCardState(child, project);
    assert.equal(view.canCreate, false);
    assert.match(view.reason, /Card 1/);
    const available = {...project, cards: [{...first, ready_hires: {id: "hr1"}}, child]};
    assert.equal(hiresCardState(child, available).canCreate, true);
});

test("image restart starts a fresh chain", () => {
    const view = hiresCardState({...child, continuation_strategy: "image_restart"}, project);
    assert.equal(view.canCreate, true);
    assert.equal(view.parentLabel, "New hires chain");
});

test("off, pending operations, drafts and batches disable creation", () => {
    assert.equal(hiresCardState(first, {...project, hires_cadence: "off"}).canCreate, false);
    assert.equal(hiresCardState(first, {...project, pending_operation: {kind: "hires_refine"}}).canCreate, false);
    assert.equal(hiresCardState(first, project, {batchActive: true}).canCreate, false);
    assert.equal(hiresCardState({...first, status: "DRAFT"}, project).visible, false);
});

test("failed retries still allow preview of the previous ready derivative", () => {
    const view = hiresCardState({...first, hires: {status: "FAILED"}, ready_hires: {id: "old", status: "READY"}}, project);
    assert.equal(view.status, "FAILED");
    assert.equal(view.canPreview, true);
    assert.equal(view.canDelete, true);
    assert.equal(view.ready.id, "old");
});

test("invalidated derivatives cannot be previewed", () => {
    const view = hiresCardState({...first, hires: {status: "INVALIDATED"}}, project);
    assert.equal(view.canPreview, false);
    assert.equal(view.canCreate, true);
    assert.equal(view.canDelete, true);
});

test("target dimensions follow node settings before the first upscale", () => {
    const view = hiresCardState(first, project, {width: 1632, height: 2880, scale: 3});
    assert.deepEqual([view.width, view.height, view.scale], [1632, 2880, 3]);
});

const hiresDefaults = {
    hires_upscaler_model: "learned.safetensors", hires_scale: 2,
    hires_target_width: 0, hires_target_height: 0, hires_temporal_chunking: true,
    hires_precision: "bf16", hires_card_id: "",
};
const definitions = {hires_upscaler_model: [["learned.safetensors"]]};

test("old null button slots receive valid defaults for all hires widgets", () => {
    const node = {widgets: Object.keys(hiresDefaults).map(name => ({name, value: null}))};
    assert.equal(repairHiresWidgetDefaults(node, definitions), true);
    assert.deepEqual(Object.fromEntries(node.widgets.map(w => [w.name, w.value])), hiresDefaults);
    assert.equal(repairHiresWidgetDefaults(node, definitions), false);
});

test("undefined and nonfinite numeric values never serialize as null", () => {
    for (const missing of [undefined, NaN, Infinity]) {
        const node = {widgets: ["hires_scale", "hires_target_width", "hires_target_height"].map(name => ({name, value: missing}))};
        repairHiresWidgetDefaults(node);
        assert.equal(JSON.stringify(node.widgets.map(w => w.value)), "[2,0,0]");
    }
});

test("repair preserves explicit hires values, native settings and connection sockets", () => {
    const node = {widgets: [
        {name: "hires_scale", value: 3}, {name: "hires_target_width", value: 0},
        {name: "hires_target_height", value: 1920}, {name: "hires_temporal_chunking", value: false},
        {name: "hires_upscaler_model", value: "chosen.safetensors"}, {name: "hires_precision", value: "fp16"},
        {name: "hires_card_id", value: "accepted"}, {name: "seed", value: 17},
        {name: "hires_model", value: null}, {name: "hires_sigmas", value: null},
    ]};
    const before = structuredClone(node);
    assert.equal(repairHiresWidgetDefaults(node, definitions), false);
    assert.deepEqual(node, before);
});

test("actual extension repairs both loading and normal queue serialization", async () => {
    let extension;
    // Exercise the registered node hooks with synthetic widgets, without
    // loading a saved graph or invoking app.queuePrompt.
    const source = readFileSync(new URL("../web/longcaster.js", import.meta.url), "utf8")
        .replace(/^import .*;\r?\n/gm, "");
    vm.runInNewContext(source, {
        app: {registerExtension: value => { extension = value; }},
        api: {}, repairHiresWidgetDefaults,
    });
    class Node {
        constructor() { this.widgets = []; }
        onConfigure(values) { this.widgets = values; return "configured"; }
        serialize() { return {widgets_values: this.widgets.map(w => w.value)}; }
    }
    await extension.beforeRegisterNodeDef(Node, {name: "LongCasterProject", input: {optional: definitions}});
    const node = new Node();
    assert.equal(node.onConfigure(Object.keys(hiresDefaults).map(name => ({name, value: null}))), "configured");
    assert.deepEqual(node.serialize().widgets_values, Object.values(hiresDefaults));
    // Simulate a later widget restore, then serialize through the normal queue path.
    node.widgets.forEach(w => { w.value = null; });
    assert.deepEqual(node.serialize().widgets_values, Object.values(hiresDefaults));
});
