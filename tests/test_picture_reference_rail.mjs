import assert from "node:assert/strict";
import {test} from "node:test";
import {assemblePictureReferences, createPictureReferenceRail} from "../web/picture_reference_rail.js";

class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.hidden = false; this.className = ""; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
}
globalThis.document = {createElement: tag => new Element(tag)};

test("orders global, enabled card, and identity pictures", () => {
    const result = assemblePictureReferences({
        globalPictures: [{label: "Character"}, {label: "Location"}],
        cardReference: {enabled: true, label: "Pose"},
        identityReference: {enabled: true, label: "Identity"},
    });
    assert.equal(result.count, 4);
    assert.deepEqual(result.active.map(item => [item.source, item.pictureIndex]), [
        ["project", 1], ["project", 2], ["card", 3], ["identity", 4],
    ]);
});

test("disabled card remains visible without consuming a picture number", () => {
    const result = assemblePictureReferences({
        globalPictures: [{label: "Character"}],
        cardReference: {enabled: false, label: "Pose"},
        identityReference: {enabled: true, label: "Identity"},
    });
    assert.equal(result.count, 2);
    assert.deepEqual(result.active.map(item => item.pictureIndex), [1, 2]);
    assert.equal(result.inactive[0].pictureIndex, null);
});

test("rail renders thumbnails, placeholders, and toggle state", () => {
    const result = assemblePictureReferences({
        globalPictures: [{label: "Character", thumbnail: "/character.png"}],
        cardReference: {enabled: true, label: "Pose"},
    });
    const rail = createPictureReferenceRail();
    rail.render(result, {open: true, sourceNote: "connected graph"});
    assert.equal(rail.root.hidden, false);
    assert.equal(rail.root.children[0].children[1].textContent, "2 active · connected graph");
    const tiles = rail.root.children[1].children;
    assert.equal(tiles[0].children[0].src, "/character.png");
    assert.equal(tiles[0].children[1].textContent, "<Picture 1>");
    assert.equal(tiles[1].children[0].textContent, "IMG");
    rail.render(result, {open: false});
    assert.equal(rail.root.hidden, true);
});
