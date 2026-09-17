import assert from "node:assert/strict";
import {test} from "node:test";
import {createCardReferenceControls, referenceSettings} from "../web/card_reference.js";

class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.value = ""; }
    append(...children) { this.children.push(...children); }
    removeAttribute(name) { delete this[name]; }
    click() { this.clicked = true; }
}
globalThis.document = {createElement: (tag) => new Element(tag)};

function setup() {
    const events = [];
    const controls = createCardReferenceControls({
        onSettings: value => events.push(["settings", value]),
        onUpload: value => events.push(["upload", value]),
        onRemove: () => events.push(["remove"]),
    });
    const props = {project: "one", generationMode: "ref2va", editable: true,
        observedReferences: {resources: [{kind: "image"}, {kind: "video"}, {kind: "image"}]},
        card: {id: "card1", status: "DRAFT", card_reference: {id: "image1", enabled: true, latent_mode: "soften", latent_strength: 0.35}}};
    controls.render(props);
    const [title, thumbnail, label, upload, replace, remove, enabledLabel, mode, strengthLabel, slider, number] = controls.root.children;
    return {controls, props, events, thumbnail, label, upload, replace, remove, enabled: enabledLabel.children[0], mode, slider, number};
}

test("upload, replace and remove emit actions only when editable", () => {
    const ui = setup();
    assert.equal(ui.replace.textContent, "Replace");
    ui.replace.onclick(); assert.equal(ui.upload.clicked, true);
    const file = {name: "pose.png"}; ui.upload.files = [file]; ui.upload.onchange();
    ui.remove.onclick();
    assert.deepEqual(ui.events, [["upload", file], ["remove"]]);
    assert.equal(ui.upload.value, "");
});

test("strength persists through settings callback used by autosave and dirty state", () => {
    const ui = setup();
    ui.slider.value = "0.5"; ui.slider.oninput();
    assert.deepEqual(ui.events[0], ["settings", {enabled: true, latent_mode: "soften", latent_strength: 0.5}]);
    Object.assign(ui.props.card.card_reference, ui.events[0][1]);
    ui.controls.render(ui.props);
    assert.equal(ui.number.value, "0.5");
    assert.equal(referenceSettings(ui.props.card.card_reference).latent_strength, 0.5);
    ui.number.value = "1.5"; ui.number.onchange(); assert.equal(ui.events.length, 1);
});

test("native picture label counts images and accepted label uses frozen index", () => {
    const ui = setup(); assert.match(ui.label.textContent, /<Picture 3>/);
    ui.props.card.status = "ACCEPTED";
    ui.props.card.card_reference_resolved = {id: "image1", enabled: true, resolved_picture_index: 2};
    ui.props.editable = false; ui.controls.render(ui.props);
    assert.match(ui.label.textContent, /<Picture 2> \(frozen\)/);
    for (const control of [ui.replace, ui.remove, ui.enabled, ui.mode, ui.slider, ui.number]) assert.equal(control.disabled, true);
    ui.remove.onclick(); ui.mode.onchange(); assert.equal(ui.events.length, 0);
});

test("T2VA disables reference controls with a reason", () => {
    const ui = setup(); ui.props.generationMode = "t2va"; ui.controls.render(ui.props);
    assert.match(ui.label.textContent, /require REF2VA/);
    assert.equal(ui.replace.disabled, true); assert.equal(ui.slider.disabled, true);
    ui.slider.oninput(); assert.equal(ui.events.length, 0);
});

test("switching projects and cards resets thumbnails, strength, and event state", () => {
    const ui = setup();
    ui.controls.render({...ui.props, project: "two", card: {id: "card2", status: "EMPTY"}});
    assert.equal(ui.thumbnail.hidden, true); assert.equal(ui.thumbnail.src, undefined);
    assert.equal(ui.slider.value, "1"); assert.equal(ui.replace.textContent, "Add Image");
    assert.equal(ui.label.textContent, "No card reference");
    ui.mode.onchange(); assert.equal(ui.events.length, 0);
    ui.controls.render({...ui.props, project: "two", card: {...ui.props.card, id: "card2"}});
    assert.match(ui.thumbnail.src, /project=two&card_id=card2/);
});

test("disabled references have no prompt label and normal mode disables strength", () => {
    const ui = setup(); ui.props.card.card_reference.enabled = false;
    ui.props.card.card_reference.latent_mode = "normal"; ui.controls.render(ui.props);
    assert.equal(ui.label.textContent, "Card reference disabled"); assert.equal(ui.slider.disabled, true);
});

test("a file picker opened on another project cannot attach its image here", () => {
    const ui = setup(); ui.replace.onclick();
    ui.controls.render({...ui.props, project: "two", card: {...ui.props.card, id: "card2"}});
    ui.upload.files = [{name: "old-project.png"}]; ui.upload.onchange();
    assert.deepEqual(ui.events, []);
});
