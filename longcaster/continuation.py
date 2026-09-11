from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .mmh3_adapter import prepare_continuation


class ContinuationStrategy(Protocol):
    name: str

    def prepare(
        self,
        source_packet: Any,
        *,
        target_frames: int,
        width: int,
        height: int,
        context_frames: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ...


@dataclass(frozen=True)
class DirectLatentContinuation:
    """MMH3 joint AV continuation without a VAE round trip."""

    name: str = "direct_latent"

    def prepare(
        self,
        source_packet: Any,
        *,
        target_frames: int,
        width: int,
        height: int,
        context_frames: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return prepare_continuation(
            source_packet,
            target_frames=target_frames,
            width=width,
            height=height,
            context_frames=context_frames,
        )


DIRECT_LATENT_CONTINUATION = DirectLatentContinuation()
