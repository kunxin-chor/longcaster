export function generationParents(cards, card) {
    return cards.filter(item => item.status === "ACCEPTED" && item.timeline_index < card.timeline_index);
}

export function setGenerationParent(card, parent, strategy = card.continuation_strategy) {
    card.generation_parent_id = parent?.id || null;
    card.continuation_strategy = parent ? (strategy === "image_restart" ? strategy : "direct_mmh3") : "independent";
    card.color_anchor_source_card_id = card.continuation_strategy === "direct_mmh3"
        ? parent.color_anchor_source_card_id || parent.id : card.id;
}

export function generationHistory(cards, selectedId) {
    const index = new Map(cards.map(card => [card.id, card]));
    const ancestry = [], visited = new Set();
    let card = index.get(selectedId), missingId = null, cycle = false;
    while (card) {
        if (visited.has(card.id)) { cycle = true; break; }
        visited.add(card.id); ancestry.unshift(card);
        const parentId = card.generation_parent_id;
        card = index.get(parentId);
        if (parentId && !card) missingId = parentId;
    }
    return {ancestry, missingId, cycle, children: cards.filter(item => item.generation_parent_id === selectedId)};
}
