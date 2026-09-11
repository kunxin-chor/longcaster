from __future__ import annotations

from typing import Any


def selected_cards(manifest: dict[str, Any], include_active_draft: bool) -> list[dict[str, Any]]:
    """Return timeline-ordered accepted cards and, optionally, the active draft."""
    cards = [card for card in manifest["cards"] if card["status"] == "ACCEPTED"]
    if include_active_draft:
        active = next(card for card in manifest["cards"] if card["id"] == manifest["active_card_id"])
        if active["status"] == "DRAFT":
            cards.append(active)
    return sorted(cards, key=lambda card: int(card["timeline_index"]))


def retained_frame_span(card: dict[str, Any], decoded_frame_count: int) -> tuple[int, int]:
    """Return the decoded frame slice after removing continuation context."""
    context = int(card.get("context_frame_count", 0) or 0)
    if context < 0 or context >= decoded_frame_count:
        raise ValueError(f"invalid context_frame_count={context} for {decoded_frame_count} decoded frames")
    return context, decoded_frame_count
