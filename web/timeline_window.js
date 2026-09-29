// Organization-only timeline window. Generation controls stay in Studio.
export function createTimelineWindow({document = globalThis.document, onChange, onOpenCard}) {
    const el = (tag, text = "") => {
        const node = document.createElement(tag);
        node.textContent = text;
        return node;
    };
    const root = el("dialog"); root.className = "lc-timeline-window";
    root.setAttribute("aria-label", "Timelines");
    const style = el("style", `
        .lc-timeline-window{width:min(1000px,90vw);max-height:85vh;box-sizing:border-box;overflow:auto;background:#0a1824;color:#dce8f3;border:1px solid #31516a;border-radius:10px;padding:20px;font:13px system-ui}
        .lc-timeline-window::backdrop{background:#0009}.lc-timeline-window header{display:flex;align-items:center;gap:12px}.lc-timeline-window h2{margin:0 auto 0 0}
        .lc-timeline-window button,.lc-timeline-window select,.lc-timeline-window input{padding:8px;border:1px solid #31516a;border-radius:5px;background:#173149;color:#dce8f3}.lc-timeline-window button{cursor:pointer}.lc-timeline-window :disabled{opacity:.45;cursor:default}
        .lc-timeline-window .tools{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.lc-timeline-window input{min-width:180px;flex:1}.lc-timeline-window details{border-top:1px solid #254052;margin-top:12px;padding:10px 0}.lc-timeline-window summary{font-weight:600;cursor:pointer;overflow-wrap:anywhere}
        .lc-timeline-window .shot{display:grid;grid-template-columns:minmax(120px,1fr) minmax(100px,230px) auto;gap:10px;align-items:center;padding:8px 0}.lc-timeline-window .shot span{overflow-wrap:anywhere}.lc-timeline-window select{min-width:0;max-width:100%}.lc-timeline-window .help{color:#9ab1c2}.lc-timeline-window .error{color:#ff9292}
    `);
    const header = el("header"), title = el("h2", "Timelines"), close = el("button", "Back to Generations");
    header.append(title, close);
    const help = el("p", "Organize cards into timelines here. Generation folders and ancestry are managed in Generations. Preview, batch, and export still use project card order."); help.className = "help";
    const tools = el("div"); tools.className = "tools";
    const manager = el("select"); manager.setAttribute("aria-label", "Manage timeline");
    const create = el("button", "New Timeline"), rename = el("button", "Rename"), remove = el("button", "Delete");
    const search = el("input"); search.type = "search"; search.placeholder = "Find a card"; search.setAttribute("aria-label", "Find a timeline card");
    tools.append(manager, create, rename, remove, search);
    const list = el("div"), message = el("p"); message.setAttribute("role", "status");
    root.append(style, header, help, tools, list, message);
    document.body.append(root);
    let state = null, locked = false, busy = false, managedId = null;
    const collapsed = new Set();
    const option = (value, text) => { const node = el("option", text); node.value = value; return node; };
    function render() {
        if (!state || !root.open) return;
        title.textContent = `Timelines · ${state.project}`;
        const groups = state.organization_timelines || [];
        const project = state.project;
        if (!groups.some(group => group.id === managedId)) managedId = groups[0]?.id || null;
        manager.replaceChildren(...(groups.length ? groups.map(group => option(group.id, group.name)) : [option("", "No timelines yet")]));
        manager.value = managedId || "";
        create.disabled = busy || locked;
        manager.disabled = rename.disabled = remove.disabled = busy || locked || !groups.length;
        list.replaceChildren();
        const query = search.value.trim().toLowerCase();
        for (const group of [{id: null, name: "Unassigned"}, ...groups]) {
            const members = state.cards.filter(card => (card.organization_timeline_id || null) === group.id);
            const matches = members.filter(card => `${card.timeline_index + 1} ${card.id} ${card.title || ""} ${card.assembled_prompt || ""}`.toLowerCase().includes(query));
            const section = el("details"); section.open = query ? true : !collapsed.has(group.id);
            section.append(el("summary", `${group.name} (${query ? `${matches.length}/` : ""}${members.length})`));
            section.addEventListener("toggle", () => {
                if (!section.isConnected || query) return;
                if (section.open) collapsed.delete(group.id); else collapsed.add(group.id);
            });
            for (const card of matches) {
                const row = el("div"); row.className = "shot";
                const label = el("span", `Card ${card.timeline_index + 1} · ${card.title || "Untitled"} · ${card.status}`);
                const assignment = el("select"); assignment.setAttribute("aria-label", `Timeline for Card ${card.timeline_index + 1}`);
                assignment.append(option("", "Unassigned"), ...groups.map(item => option(item.id, item.name)));
                assignment.value = card.organization_timeline_id || ""; assignment.disabled = busy || locked;
                assignment.onchange = () => change({action: "assign", card_id: card.id, timeline_id: assignment.value || null});
                const open = el("button", "Open in Generations");
                open.onclick = async () => { if (await onOpenCard(card.id, project)) root.close(); };
                row.append(label, assignment, open); section.append(row);
            }
            if (!matches.length) section.append(el("p", query ? "No matching cards." : "No cards in this timeline."));
            list.append(section);
        }
    }
    async function change(payload) {
        if (busy || locked || !state) return;
        const project = state.project;
        busy = true; render(); message.textContent = "Saving…";
        try {
            const result = await onChange({...payload, project});
            if (state.project !== project) return;
            message.className = result?.ok ? "" : "error";
            message.textContent = result?.message || "Timeline update failed.";
            if (result?.ok && payload.action === "create") managedId = state.organization_timelines.at(-1)?.id || null;
            if (result?.ok && payload.action === "assign") collapsed.delete(payload.timeline_id);
        } catch (error) {
            if (state.project === project) { message.className = "error"; message.textContent = error.message; }
        } finally { busy = false; render(); }
    }
    close.onclick = () => root.close();
    manager.onchange = () => { managedId = manager.value || null; render(); };
    search.oninput = render;
    create.onclick = () => { const name = globalThis.prompt("Timeline name"); if (name !== null) return change({action: "create", name}); };
    rename.onclick = () => {
        const group = state?.organization_timelines?.find(item => item.id === managedId);
        if (!group) return;
        const name = globalThis.prompt("Timeline name", group.name);
        if (name !== null) return change({action: "rename", timeline_id: group.id, name});
    };
    remove.onclick = () => {
        const group = state?.organization_timelines?.find(item => item.id === managedId);
        if (group && globalThis.confirm(`Delete timeline "${group.name}"? Its cards will become Unassigned. Media and generation folders will be kept.`)) return change({action: "delete", timeline_id: group.id});
    };
    return {
        root,
        update(next, options = {}) {
            if (state?.project !== next.project) { managedId = null; collapsed.clear(); search.value = ""; message.textContent = ""; }
            state = next; locked = Boolean(options.locked); render();
        },
        open() { if (!root.open) root.showModal(); render(); },
        close() { if (root.open) root.close(); },
    };
}
