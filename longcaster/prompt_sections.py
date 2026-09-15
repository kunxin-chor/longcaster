from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any, Mapping


PROMPT_SECTION_NAMES = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
PROMPT_SOURCE_TYPES = {
    "manual",
    "copied_previous",
    "copied_card",
    "generated_internal",
}


def section_record(
    text: str = "",
    *,
    source_type: str = "manual",
    source_card_id: str | None = None,
    modified_after_copy: bool = False,
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("prompt section text must be a string")
    if source_type not in PROMPT_SOURCE_TYPES:
        raise ValueError(f"unsupported prompt section source_type: {source_type}")
    if source_card_id is not None and not isinstance(source_card_id, str):
        raise ValueError("prompt section source_card_id must be a string or null")
    if source_type in {"copied_previous", "copied_card"} and not source_card_id:
        raise ValueError("copied prompt sections require source_card_id")
    if source_type not in {"copied_previous", "copied_card"} and source_card_id is not None:
        raise ValueError("only copied prompt sections may have source_card_id")
    if not isinstance(modified_after_copy, bool):
        raise ValueError("modified_after_copy must be boolean")
    return {
        "text": text,
        "provenance": {
            "source_type": source_type,
            "source_card_id": source_card_id,
            "modified_after_copy": modified_after_copy,
        },
    }


def empty_prompt_sections() -> dict[str, dict[str, Any]]:
    return {name: section_record() for name in PROMPT_SECTION_NAMES}


def validate_prompt_sections(sections: Mapping[str, Any]) -> None:
    if not isinstance(sections, Mapping) or set(sections.keys()) != set(PROMPT_SECTION_NAMES):
        raise ValueError("prompt_sections must contain the six canonical sections in order")
    for name in PROMPT_SECTION_NAMES:
        record = sections[name]
        if not isinstance(record, Mapping):
            raise ValueError(f"prompt section {name} must be an object")
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"prompt section {name} provenance must be an object")
        expected = section_record(
            record.get("text"),
            source_type=provenance.get("source_type"),
            source_card_id=provenance.get("source_card_id"),
            modified_after_copy=provenance.get("modified_after_copy"),
        )
        if set(record.keys()) != set(expected.keys()) or set(provenance.keys()) != set(
            expected["provenance"].keys()
        ):
            raise ValueError(f"prompt section {name} contains unsupported fields")


def assemble_prompt(sections: Mapping[str, Any]) -> str:
    validate_prompt_sections(sections)
    return "\n\n".join(
        f"{name}:" + (f"\n{sections[name]['text']}" if sections[name]["text"] else "")
        for name in PROMPT_SECTION_NAMES
    )


def hash_prompt(prompt: str) -> str:
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def inject_lora_activation_words(prompt: str, activation_words: str) -> str:
    """Inject exact project LoRA triggers without mutating saved prompt sections."""
    words = str(activation_words).strip()
    if not words:
        return prompt
    heading = "subject_definitions:"
    if prompt.startswith(heading):
        tail = prompt[len(heading):]
        if tail.startswith("\r\n"):
            return f"{heading}\r\n{words}{tail}"
        if tail.startswith("\n"):
            return f"{heading}\n{words}{tail}"
        return f"{heading}\n{words}\n{tail}" if tail else f"{heading}\n{words}"
    return f"{words}\n\n{prompt}" if prompt else words


def _remove_framing_newlines(value: str) -> str:
    opening = "\r\n" if value.startswith("\r\n") else "\n" if value.startswith("\n") else ""
    closing = "\r\n" if value.endswith("\r\n") else "\n" if value.endswith("\n") else ""
    if opening and closing:
        return value[len(opening):len(value) - len(closing)]
    return value


def parse_legacy_prompt(prompt: str) -> dict[str, dict[str, Any]] | None:
    """Parse the obsolete XML-style form so existing projects can migrate safely."""
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    records: dict[str, dict[str, Any]] = {}
    position = 0
    for name in PROMPT_SECTION_NAMES:
        if len(re.findall(rf"<{name}\b", prompt, re.IGNORECASE)) != 1:
            return None
        if len(re.findall(rf"</{name}\s*>", prompt, re.IGNORECASE)) != 1:
            return None
        pattern = re.compile(
            rf"<{name}\b[^>]*>(.*?)</{name}\s*>", re.IGNORECASE | re.DOTALL
        )
        matches = list(pattern.finditer(prompt))
        if len(matches) != 1:
            return None
        match = matches[0]
        if match.start() < position or prompt[position:match.start()].strip():
            return None
        records[name] = section_record(
            _remove_framing_newlines(match.group(1)), source_type="generated_internal"
        )
        position = match.end()
    if prompt[position:].strip():
        return None
    return records


def parse_labeled_prompt(prompt: str) -> dict[str, dict[str, Any]] | None:
    """Parse the common ``section_name:`` flat format when all six labels are unambiguous."""
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    def label_pattern(name: str) -> str:
        return r"[ _-]+".join(re.escape(part) for part in name.split("_"))

    labels = {
        name: re.compile(
            rf"(?im)^[ \t]*(?:#+[ \t]*)?{label_pattern(name)}[ \t]*:[ \t]*$"
        )
        for name in PROMPT_SECTION_NAMES
    }
    matches = []
    for name in PROMPT_SECTION_NAMES:
        found = list(labels[name].finditer(prompt))
        if len(found) != 1:
            return None
        matches.append((name, found[0]))
    if [match.start() for _, match in matches] != sorted(match.start() for _, match in matches):
        return None
    if prompt[:matches[0][1].start()].strip():
        return None
    records: dict[str, dict[str, Any]] = {}
    for index, (name, match) in enumerate(matches):
        end = matches[index + 1][1].start() if index + 1 < len(matches) else len(prompt)
        value = prompt[match.end():end].strip("\r\n")
        records[name] = section_record(value, source_type="generated_internal")
    return records


def suggested_prompt_sections(prompt: str) -> dict[str, dict[str, Any]] | None:
    """Return a safe automatic conversion for tagged or canonical labelled prompts."""
    return parse_legacy_prompt(prompt) or parse_labeled_prompt(prompt)


def imported_prompt_sections(prompt: str) -> dict[str, dict[str, Any]]:
    """Split pasted text by unique canonical labels, falling back to description."""
    if not isinstance(prompt, str):
        raise ValueError("imported prompt must be a string")
    complete = suggested_prompt_sections(prompt)
    if complete is not None:
        return complete
    if not prompt:
        return empty_prompt_sections()

    matches: list[tuple[str, re.Match[str]]] = []
    for name in PROMPT_SECTION_NAMES:
        label = r"[ _-]+".join(re.escape(part) for part in name.split("_"))
        found = list(
            re.finditer(
                rf"(?im)^[ \t]*(?:#+[ \t]*)?{label}[ \t]*:[ \t]*(?P<inline>[^\r\n]*)$",
                prompt,
            )
        )
        if len(found) > 1:
            raise ValueError(f"imported prompt contains more than one {name} heading")
        if found:
            matches.append((name, found[0]))
    if not matches:
        sections = empty_prompt_sections()
        sections["detailed_description"] = section_record(prompt)
        return sections

    matches.sort(key=lambda item: item[1].start())
    sections = empty_prompt_sections()
    preamble = prompt[:matches[0][1].start()].strip("\r\n")
    for index, (name, match) in enumerate(matches):
        end = matches[index + 1][1].start() if index + 1 < len(matches) else len(prompt)
        inline = match.group("inline").rstrip()
        following = prompt[match.end():end].strip("\r\n")
        sections[name] = section_record(
            f"{inline}\n{following}" if inline and following else inline or following
        )
    if preamble:
        detail = sections["detailed_description"]["text"]
        sections["detailed_description"] = section_record(
            f"{preamble}\n\n{detail}" if detail else preamble
        )
    return sections


def prompt_fields(prompt: str) -> dict[str, Any]:
    parsed = parse_legacy_prompt(prompt)
    if parsed is None:
        return {
            "prompt_sections": empty_prompt_sections(),
            "prompt_format": "legacy_flat",
            "assembled_prompt": prompt,
            "prompt_hash": hash_prompt(prompt),
        }
    assembled = assemble_prompt(parsed)
    return {
        "prompt_sections": parsed,
        "prompt_format": "structured_v1",
        "assembled_prompt": assembled,
        "prompt_hash": hash_prompt(assembled),
    }


def inherited_prompt_sections(
    source_sections: Mapping[str, Any], source_card_id: str
) -> dict[str, dict[str, Any]]:
    validate_prompt_sections(source_sections)
    inherited = empty_prompt_sections()
    copied = {
        "subject_definitions",
        "retention_analysis",
        "overall_soundscape",
        "non_diegetic_music",
    }
    for name in copied:
        inherited[name] = section_record(
            source_sections[name]["text"],
            source_type="copied_previous",
            source_card_id=source_card_id,
        )
    return inherited


def copy_sections(
    target_sections: Mapping[str, Any],
    source_sections: Mapping[str, Any],
    *,
    source_card_id: str,
    names: list[str] | tuple[str, ...],
    previous: bool = False,
) -> dict[str, dict[str, Any]]:
    validate_prompt_sections(target_sections)
    validate_prompt_sections(source_sections)
    invalid = set(names) - set(PROMPT_SECTION_NAMES)
    if invalid:
        raise ValueError(f"unsupported prompt sections: {', '.join(sorted(invalid))}")
    result = deepcopy(target_sections)
    source_type = "copied_previous" if previous else "copied_card"
    for name in names:
        result[name] = section_record(
            source_sections[name]["text"],
            source_type=source_type,
            source_card_id=source_card_id,
        )
    return result


def edit_sections(
    sections: Mapping[str, Any], changes: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    validate_prompt_sections(sections)
    invalid = set(changes) - set(PROMPT_SECTION_NAMES)
    if invalid:
        raise ValueError(f"unsupported prompt sections: {', '.join(sorted(invalid))}")
    result = deepcopy(sections)
    for name, text in changes.items():
        if not isinstance(text, str):
            raise ValueError(f"prompt section {name} text must be a string")
        current = result[name]
        if current["text"] == text:
            continue
        provenance = current["provenance"]
        if provenance["source_type"] in {"copied_previous", "copied_card"}:
            result[name] = section_record(
                text,
                source_type=provenance["source_type"],
                source_card_id=provenance["source_card_id"],
                modified_after_copy=True,
            )
        else:
            result[name] = section_record(text)
    return result
