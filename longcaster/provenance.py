from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any


PROVENANCE_VERSION = 1
GENERATION_ROOT_INPUTS = (
    "model",
    "clip",
    "video_vae",
    "audio_vae",
    "sigmas",
    "reference_packet",
)
_MAX_DEPTH = 8
_MAX_ITEMS = 128
_MAX_NODES = 512
_MAX_STRING = 4096
_REDACTED_INPUT_NAMES = {
    "api_key", "apikey", "auth_token", "authorization", "cookie", "password",
    "secret", "token",
}
_MODEL_NAME_INPUTS = (
    "ckpt_name", "unet_name", "diffusion_model_name", "model_name", "checkpoint",
)
_LORA_NAME_INPUTS = ("lora_name", "lora", "lora_file")


def _node(graph: Mapping[Any, Any], node_id: Any) -> Mapping[str, Any] | None:
    value = graph.get(str(node_id), graph.get(node_id))
    return value if isinstance(value, Mapping) else None


def _link(value: Any, graph: Mapping[Any, Any]) -> tuple[str, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    source, slot = value
    if not isinstance(slot, int) or _node(graph, source) is None:
        return None
    return str(source), slot


def _safe_value(value: Any, *, name: str = "", depth: int = 0) -> tuple[Any, bool]:
    if name.lower() in _REDACTED_INPUT_NAMES:
        return "[redacted]", True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        if len(value) <= _MAX_STRING:
            return value, False
        return value[:_MAX_STRING] + "…[truncated]", True
    if depth >= _MAX_DEPTH:
        return "[maximum depth omitted]", True
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        truncated = len(value) > _MAX_ITEMS
        for key, item in list(value.items())[:_MAX_ITEMS]:
            safe, changed = _safe_value(item, name=str(key), depth=depth + 1)
            result[str(key)] = safe
            truncated = truncated or changed
        if len(value) > _MAX_ITEMS:
            result["__omitted_items__"] = len(value) - _MAX_ITEMS
        return result, truncated
    if isinstance(value, (list, tuple)):
        result = []
        truncated = len(value) > _MAX_ITEMS
        for item in list(value)[:_MAX_ITEMS]:
            safe, changed = _safe_value(item, depth=depth + 1)
            result.append(safe)
            truncated = truncated or changed
        if len(value) > _MAX_ITEMS:
            result.append({"__omitted_items__": len(value) - _MAX_ITEMS})
        return result, truncated
    return f"[{type(value).__name__} omitted]", True


def _settings(node: Mapping[str, Any], graph: Mapping[Any, Any]) -> tuple[dict[str, Any], bool]:
    values: dict[str, Any] = {}
    truncated = False
    inputs = node.get("inputs")
    if not isinstance(inputs, Mapping):
        return values, truncated
    for name, value in inputs.items():
        link = _link(value, graph)
        if link is not None:
            values[str(name)] = {"node_id": link[0], "output": link[1]}
            continue
        safe, changed = _safe_value(value, name=str(name))
        values[str(name)] = safe
        truncated = truncated or changed
    return values, truncated


def _display_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in settings.items()
        if not (isinstance(value, Mapping) and "node_id" in value and "output" in value)
    }


def _summary_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "node_id": record["node_id"],
        "class_type": record["class_type"],
        "title": record.get("title"),
        "settings": _display_settings(record.get("inputs", {})),
    }


def _first_named_setting(settings: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    lowered = {str(key).lower(): value for key, value in settings.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def capture_generation_provenance(
    prompt: Any,
    unique_id: Any,
    *,
    root_inputs: tuple[str, ...] = GENERATION_ROOT_INPUTS,
) -> dict[str, Any] | None:
    """Capture prompt JSON upstream of inputs that can affect a first-pass draft."""
    if not isinstance(prompt, Mapping):
        return None
    target = _node(prompt, unique_id)
    if target is None or not isinstance(target.get("inputs"), Mapping):
        return None

    roots: dict[str, dict[str, Any]] = {}
    branches: dict[str, set[str]] = {}
    target_inputs = target["inputs"]
    for input_name in root_inputs:
        link = _link(target_inputs.get(input_name), prompt)
        if link is not None:
            roots[input_name] = {"node_id": link[0], "output": link[1]}

    ordered: list[str] = []
    all_seen: set[str] = set()
    discovered: set[str] = set()
    node_limit_reached = False

    def visit(node_id: str, branch: set[str], visiting: set[str]) -> None:
        nonlocal node_limit_reached
        if node_id in branch or node_id in visiting:
            return
        if node_id not in discovered:
            if len(discovered) >= _MAX_NODES:
                node_limit_reached = True
                return
            discovered.add(node_id)
        visiting.add(node_id)
        current = _node(prompt, node_id)
        if current is not None and isinstance(current.get("inputs"), Mapping):
            for value in current["inputs"].values():
                link = _link(value, prompt)
                if link is not None:
                    visit(link[0], branch, visiting)
        visiting.discard(node_id)
        branch.add(node_id)
        if node_id not in all_seen:
            all_seen.add(node_id)
            ordered.append(node_id)

    for input_name, root in roots.items():
        branch: set[str] = set()
        visit(root["node_id"], branch, set())
        branches[input_name] = branch

    records: list[dict[str, Any]] = []
    truncated = node_limit_reached
    for node_id in ordered:
        current = _node(prompt, node_id)
        if current is None:
            continue
        inputs, changed = _settings(current, prompt)
        truncated = truncated or changed
        meta = current.get("_meta")
        title = meta.get("title") if isinstance(meta, Mapping) else None
        records.append({
            "node_id": node_id,
            "class_type": str(current.get("class_type") or "Unknown"),
            "title": str(title) if title else None,
            "inputs": inputs,
        })

    by_id = {record["node_id"]: record for record in records}
    model_path = [by_id[item] for item in ordered if item in branches.get("model", set())]
    models: list[dict[str, Any]] = []
    loras: list[dict[str, Any]] = []
    patches: list[dict[str, Any]] = []
    for record in model_path:
        lowered = record["class_type"].lower()
        settings = _display_settings(record["inputs"])
        lora_name = _first_named_setting(settings, _LORA_NAME_INPUTS)
        model_name = _first_named_setting(settings, _MODEL_NAME_INPUTS)
        entry = _summary_entry(record)
        if "lora" in lowered or lora_name is not None:
            entry["name"] = lora_name
            loras.append(entry)
        elif model_name is not None or "checkpointloader" in lowered or "unetloader" in lowered:
            entry["name"] = model_name
            models.append(entry)
        else:
            patches.append(entry)

    content = {
        "version": PROVENANCE_VERSION,
        "roots": roots,
        "branches": {name: [item for item in ordered if item in ids] for name, ids in branches.items()},
        "nodes": records,
        "summary": {"models": models, "loras": loras, "patches": patches},
        "truncated": truncated,
    }
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content["graph_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    content["stored_bytes"] = len(canonical.encode("utf-8"))
    return content


def provenance_for_display(recipe: Any) -> dict[str, Any] | None:
    if not isinstance(recipe, Mapping):
        return None
    provenance = recipe.get("upstream_provenance")
    if not isinstance(provenance, Mapping):
        return None
    summary = provenance.get("summary")
    if not isinstance(summary, Mapping):
        summary = {}
    def entries(name: str) -> list[dict[str, Any]]:
        value = summary.get(name)
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return {
        "models": entries("models"),
        "loras": entries("loras"),
        "patches": entries("patches"),
        "node_count": len(provenance.get("nodes") or []),
        "graph_sha256": provenance.get("graph_sha256"),
        "stored_bytes": provenance.get("stored_bytes"),
        "truncated": bool(provenance.get("truncated", False)),
    }
