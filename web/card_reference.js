export function referenceSettings(reference) {
    return reference ? {
        enabled: Boolean(reference.enabled), latent_mode: reference.latent_mode,
        latent_strength: Number(reference.latent_strength),
    } : null;
}

export function createCardReferenceControls({onSettings, onUpload, onRemove, assetURL = path => path}) {
    const make = (tag, text = "") => {
        const element = document.createElement(tag); element.textContent = text; return element;
    };
    const root = make("div"); root.className = "lc-cards-field";
    const title = make("label", "Card Reference");
    const thumbnail = make("img"); thumbnail.alt = "Card reference";
    thumbnail.style.cssText = "width:100%;max-height:160px;object-fit:contain";
    const label = make("div"); label.className = "lc-cards-help";
    const upload = make("input"); upload.type = "file"; upload.accept = "image/*"; upload.hidden = true;
    const replace = make("button", "Add Image");
    const remove = make("button", "Remove");
    const enabled = make("input"); enabled.type = "checkbox";
    const enabledLabel = make("label"); enabledLabel.append(enabled, make("span", "Enabled"));
    const mode = make("select");
    for (const [value, text] of [["normal", "Normal"], ["soften", "Soften"]]) {
        const option = make("option", text); option.value = value; mode.append(option);
    }
    const strength = make("input"); strength.type = "range"; strength.min = "0"; strength.max = "1"; strength.step = "0.01";
    const number = make("input"); number.type = "number"; number.min = "0"; number.max = "1"; number.step = "0.01";
    const help = make("div", "Latent strength: 1 = native; 0 = zero latent. The original picture remains available to the text/vision encoder.");
    help.className = "lc-cards-help";
    root.append(title, thumbnail, label, upload, replace, remove, enabledLabel, mode, make("label", "Latent strength"), strength, number, help);
    let context = null;
    let pickerKey = null;
    function settings() {
        if (!context?.canEdit || !context.reference) return;
        onSettings({enabled: enabled.checked, latent_mode: mode.value, latent_strength: Number(number.value)});
    }
    replace.onclick = () => { if (context?.canEdit) { pickerKey = context.key; upload.click(); } };
    upload.onchange = () => {
        const file = upload.files?.[0]; upload.value = "";
        if (file && context?.canEdit && pickerKey === context.key) onUpload(file);
        pickerKey = null;
    };
    remove.onclick = () => { if (context?.canEdit && context.reference) onRemove(); };
    enabled.onchange = mode.onchange = settings;
    strength.oninput = () => { number.value = strength.value; settings(); };
    number.onchange = () => {
        const value = Number(number.value);
        if (!Number.isFinite(value) || value < 0 || value > 1 || number.value === "") return;
        strength.value = String(value); settings();
    };
    return {root, render({project, card, generationMode, editable, observedReferences}) {
        const reference = card?.card_reference;
        const canEdit = editable && generationMode === "ref2va";
        context = {reference, canEdit, key: JSON.stringify([project, card?.id])};
        upload.value = "";
        const frozen = card?.card_reference_resolved;
        const frozenIndex = frozen?.enabled && frozen.id === reference?.id ? frozen.resolved_picture_index : null;
        const liveIndex = observedReferences ? 1 + observedReferences.resources.filter(item => item.kind === "image").length : null;
        const index = card?.status === "ACCEPTED" ? frozenIndex : liveIndex;
        label.textContent = generationMode !== "ref2va" ? "Card references require REF2VA."
            : !reference ? "No card reference"
            : !reference.enabled ? "Card reference disabled"
            : index ? `Card Reference → <Picture ${index}>${card.status === "ACCEPTED" ? " (frozen)" : " (last resolved packet)"}`
            : "Picture number resolves when the connected packet executes.";
        thumbnail.hidden = !reference;
        if (reference) thumbnail.src = assetURL(`/longcaster/cards/reference?${new URLSearchParams({project, card_id: card.id, reference_id: reference.id})}`);
        else thumbnail.removeAttribute("src");
        replace.textContent = reference ? "Replace" : "Add Image";
        replace.disabled = !canEdit; remove.disabled = !canEdit || !reference;
        enabled.checked = Boolean(reference?.enabled); enabled.disabled = !canEdit || !reference;
        mode.value = reference?.latent_mode || "normal"; mode.disabled = !canEdit || !reference;
        strength.value = number.value = String(reference?.latent_strength ?? 1);
        strength.disabled = number.disabled = !canEdit || !reference || !reference.enabled || mode.value !== "soften";
    }};
}
