from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalise(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("fingerprint values must be finite")
        return float(format(value, ".17g"))
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return repr(value)


def generation_fingerprint(recipe: dict[str, Any]) -> str:
    canonical = json.dumps(
        _normalise(recipe),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
