"""Core helpers for the MiniMax H3 LongCaster custom node."""

from .duration import DurationPlan, resolve_duration
from .continuation import ContinuationStrategy, DirectLatentContinuation
from .fingerprint import generation_fingerprint
from .project import ProjectError, ProjectStore
from .prompt_sections import PROMPT_SECTION_NAMES, assemble_prompt

__all__ = [
    "DurationPlan",
    "ContinuationStrategy",
    "DirectLatentContinuation",
    "ProjectError",
    "ProjectStore",
    "PROMPT_SECTION_NAMES",
    "assemble_prompt",
    "generation_fingerprint",
    "resolve_duration",
]
