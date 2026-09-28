// Pure view state, shared with the Studio controls' CPU tests.
export function repairHiresWidgetDefaults(node, definitions = {}) {
    // Older saved nodes may restore the former action-button null slots into
    // these newly appended widgets. Repair before Comfy validates the prompt.
    const defaults = {
        hires_scale: 2.0,
        hires_target_width: 0,
        hires_target_height: 0,
        hires_temporal_chunking: true,
        hires_precision: "bf16",
        hires_card_id: "",
    };
    let changed = false;
    for (const item of node.widgets || []) {
        let fallback;
        if (item.name === "hires_upscaler_model") {
            const choices = definitions[item.name]?.[0] || item.options?.values;
            fallback = Array.isArray(choices) ? choices[0] : "(select an H3 3D upscaler model)";
        } else if (Object.hasOwn(defaults, item.name)) {
            fallback = defaults[item.name];
        } else {
            continue;
        }
        // Preserve explicit zero dimensions, false chunking, chosen models and
        // all unrelated settings. Invalid finite values still reach validation.
        if (item.value == null || (typeof fallback === "number" && typeof item.value === "number" && !Number.isFinite(item.value))) {
            item.value = fallback;
            changed = true;
        }
    }
    return changed;
}

export function hiresCardState(card, project, settings = {}) {
    const latest = card?.hires;
    const ready = card?.ready_hires;
    const parent = card?.continuation_strategy === "direct_mmh3"
        ? project.cards?.find(item => item.id === card.generation_parent_id) : null;
    const busy = Boolean(project.pending_operation || settings.batchActive);
    const scale = Number(settings.scale || 2);
    const width = Number(settings.width) || Math.round(project.width * scale / 32) * 32;
    const height = Number(settings.height) || Math.round(project.height * scale / 32) * 32;
    const missingParent = card?.continuation_strategy === "direct_mmh3" && !parent?.ready_hires;
    return {
        visible: card?.status === "ACCEPTED",
        status: latest?.status || "Not generated",
        width: latest?.target_width || width,
        height: latest?.target_height || height,
        scale: latest?.scale || scale,
        parentLabel: parent ? `Card ${parent.timeline_index + 1}` : "New hires chain",
        canCreate: card?.status === "ACCEPTED" && project.hires_cadence !== "off" && !busy && !missingParent,
        canPreview: Boolean(ready) && !busy,
        canDelete: Boolean(latest) && !busy,
        reason: missingParent ? `Requires ready Card ${(parent?.timeline_index ?? -1) + 1} high-resolution derivative.`
            : project.hires_cadence === "off" ? "High Resolution is Off." : "",
        ready,
    };
}
