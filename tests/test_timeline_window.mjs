import assert from "node:assert/strict";
import {test} from "node:test";
import {createTimelineWindow} from "../web/timeline_window.js";
import {generationHistory, generationParents, setGenerationParent} from "../web/generation_history.js";

class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.value = ""; this.open = false; this.isConnected = true; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
}

test("parent choices use earlier accepted cards across folders and support fresh starts", () => {
    const parent = {id: "a", timeline_index: 0, status: "ACCEPTED", generation_folder_id: "other", color_anchor_source_card_id: "a"};
    const unfinished = {id: "b", timeline_index: 1, status: "DRAFT"};
    const card = {id: "c", timeline_index: 2, status: "EMPTY", continuation_strategy: "independent", timeline_predecessor_id: "b"};
    const later = {id: "d", timeline_index: 3, status: "ACCEPTED"};
    assert.deepEqual(generationParents([parent, unfinished, card, later], card), [parent]);
    setGenerationParent(card, parent);
    assert.equal(card.continuation_strategy, "direct_mmh3");
    assert.equal(card.generation_parent_id, "a");
    assert.equal(card.color_anchor_source_card_id, "a");
    assert.equal(card.timeline_predecessor_id, "b");
    setGenerationParent(card, parent, "image_restart");
    assert.equal(card.continuation_strategy, "image_restart");
    assert.equal(card.color_anchor_source_card_id, "c");
    setGenerationParent(card, null);
    assert.equal(card.continuation_strategy, "independent");
    assert.equal(card.generation_parent_id, null);
    assert.equal(card.color_anchor_source_card_id, "c");
});
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function setup(onChange = async () => ({ok: true, message: "Saved"})) {
    const state = {
        project: "film", organization_timelines: [{id: "edit", name: "Episode 1"}],
        generation_folders: [{id: "scene", name: "Kitchen"}],
        cards: [{id: "a", timeline_index: 0, title: "Opening", status: "ACCEPTED", generation_folder_id: "scene", organization_timeline_id: "edit"}],
    };
    const opened = [];
    const document = {body: new Element("body"), createElement: tag => new Element(tag)};
    const ui = createTimelineWindow({document, onChange, onOpenCard: async (...args) => { opened.push(args); return true; }});
    ui.update(state); ui.open();
    return {ui, state, opened, find: predicate => descendants(ui.root).find(predicate)};
}

test("timeline assignments stay in their own window and leave generation folders untouched", async () => {
    const changes = [];
    const {ui, state, find} = setup(async payload => { changes.push(payload); return {ok: true, message: "Saved"}; });
    const select = find(item => item.attributes["aria-label"] === "Timeline for Card 1");
    select.value = ""; await select.onchange();
    assert.deepEqual(changes, [{project: "film", action: "assign", card_id: "a", timeline_id: null}]);
    assert.equal(state.cards[0].generation_folder_id, "scene");
    assert.equal(descendants(ui.root).some(item => item.textContent === "Kitchen"), false);
    assert.equal(ui.root.tag, "dialog");
});

test("opening a timeline card navigates back to Generations", async () => {
    const {ui, opened, find} = setup();
    await find(item => item.textContent === "Open in Generations").onclick();
    assert.deepEqual(opened, [["a", "film"]]);
    assert.equal(ui.root.open, false);
});

test("busy project blocks writes but keeps timeline browsing available", async () => {
    const changes = [];
    const {ui, state, find} = setup(async payload => changes.push(payload));
    ui.update(state, {locked: true});
    const select = find(item => item.attributes["aria-label"] === "Timeline for Card 1");
    assert.equal(select.disabled, true);
    await select.onchange();
    assert.deepEqual(changes, []);
    assert.notEqual(find(item => item.textContent === "Open in Generations").disabled, true);
});

test("search and project changes refresh the separate timeline window", () => {
    const {ui, find} = setup();
    const search = find(item => item.tag === "input");
    search.value = "missing"; search.oninput();
    assert.equal(find(item => item.textContent === "Open in Generations"), undefined);
    ui.update({project: "other", organization_timelines: [], cards: []});
    assert.equal(search.value, "");
    assert.equal(find(item => item.tag === "h2").textContent, "Timelines · other");
    assert.equal(find(item => item.textContent === "Episode 1"), undefined);
});

test("create, rename and delete use timeline actions only", async t => {
    const changes = [];
    const savedPrompt = globalThis.prompt, savedConfirm = globalThis.confirm;
    globalThis.prompt = () => "New name"; globalThis.confirm = () => true;
    t.after(() => { globalThis.prompt = savedPrompt; globalThis.confirm = savedConfirm; });
    const {find} = setup(async payload => { changes.push(payload); return {ok: true}; });
    for (const label of ["New Timeline", "Rename", "Delete"]) await find(item => item.textContent === label).onclick();
    assert.deepEqual(changes, [
        {project: "film", action: "create", name: "New name"},
        {project: "film", action: "rename", timeline_id: "edit", name: "New name"},
        {project: "film", action: "delete", timeline_id: "edit"},
    ]);
});

test("branch history follows parents regardless of folder or display order", () => {
    const a = {id: "a", generation_folder_id: "one"};
    const b = {id: "b", generation_parent_id: "a", generation_folder_id: "two"};
    const c = {id: "c", generation_parent_id: "b"};
    const alternate = {id: "d", generation_parent_id: "b"};
    const history = generationHistory([c, alternate, a, b], "b");
    assert.deepEqual(history.ancestry.map(card => card.id), ["a", "b"]);
    assert.deepEqual(history.children.map(card => card.id), ["c", "d"]);
    assert.equal(history.cycle, false);
    assert.equal(generationHistory([{id: "x", generation_parent_id: "x"}], "x").cycle, true);
    assert.equal(generationHistory([{id: "x", generation_parent_id: "missing"}], "x").missingId, "missing");
});
