export function assemblePictureReferences({globalPictures = [], cardReference = null, identityReference = null}) {
    let picture = 0;
    const active = [];
    const inactive = [];
    for (const item of globalPictures) {
        picture += 1;
        active.push({...item, source: "project", pictureIndex: picture});
    }
    if (cardReference) {
        if (cardReference.enabled) {
            picture += 1;
            active.push({...cardReference, source: "card", pictureIndex: picture});
        } else {
            inactive.push({...cardReference, source: "card", pictureIndex: null});
        }
    }
    if (identityReference?.enabled) {
        picture += 1;
        active.push({...identityReference, source: "identity", pictureIndex: picture});
    }
    return {active, inactive, count: picture};
}

export function createPictureReferenceRail() {
    const root = document.createElement("aside");
    root.className = "lc-picture-rail";
    const heading = document.createElement("div");
    heading.className = "lc-picture-rail-heading";
    const title = document.createElement("strong");
    title.textContent = "Picture References";
    const status = document.createElement("small");
    heading.append(title, status);
    const list = document.createElement("div");
    list.className = "lc-picture-rail-list";
    root.append(heading, list);

    function tile(item, inactive = false) {
        const entry = document.createElement("div");
        entry.className = `lc-picture-tile${inactive ? " inactive" : ""}`;
        const visual = item.thumbnail ? document.createElement("img") : document.createElement("div");
        if (item.thumbnail) {
            visual.src = item.thumbnail;
            visual.alt = item.label || (inactive ? "Inactive picture" : `<Picture ${item.pictureIndex}>`);
        } else {
            visual.className = "lc-picture-placeholder";
            visual.textContent = "IMG";
        }
        const token = document.createElement("strong");
        token.textContent = inactive ? "Inactive" : `<Picture ${item.pictureIndex}>`;
        const label = document.createElement("span");
        label.textContent = item.label || (item.source === "card" ? "Card Reference" : item.source === "identity" ? "Identity Anchor" : "Project Reference");
        const detail = document.createElement("small");
        detail.textContent = item.detail || (inactive ? "Disabled; no prompt number" : item.source);
        entry.append(visual, token, label, detail);
        return entry;
    }

    return {
        root,
        render(result, {open = true, sourceNote = ""} = {}) {
            root.hidden = !open;
            status.textContent = `${result.count} active${sourceNote ? ` · ${sourceNote}` : ""}`;
            list.replaceChildren();
            if (!result.active.length && !result.inactive.length) {
                const empty = document.createElement("div");
                empty.className = "lc-cards-help";
                empty.textContent = "No picture references";
                list.append(empty);
                return;
            }
            for (const item of result.active) list.append(tile(item));
            for (const item of result.inactive) list.append(tile(item, true));
        },
    };
}
