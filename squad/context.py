"""Deterministic bounded context. Estimates are not provider token counts."""
from __future__ import annotations

import json
import math


def estimate_tokens(text: str) -> int:
    # UTF-8 bytes / 3 is intentionally conservative for common code/English;
    # some tokenizers/languages differ. Never label this measured usage.
    return math.ceil(len(text.encode('utf-8')) / 3)


def clip(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ''
    if estimate_tokens(text) <= tokens:
        return text
    marker = '\n[omitted; retrieve the referenced artifact for details]\n'
    limit = max(0, tokens * 3 - len(marker.encode()))
    raw = text.encode()
    left = raw[:limit * 2 // 3].decode('utf-8', errors='ignore')
    right = raw[-(limit // 3):].decode('utf-8', errors='ignore') if limit // 3 else ''
    return clip_marker(left + marker + right, tokens)


def clip_marker(text: str, tokens: int) -> str:
    return text.encode()[:tokens * 3].decode('utf-8', errors='ignore')


def packet(required: str, sections: list[tuple[str, str]], limit: int) -> str:
    if estimate_tokens(required) > limit:
        raise ValueError('Goal and required instructions exceed context budget; narrow the task')
    out = required
    for i, (label, body) in enumerate(sections):
        if not body:
            continue
        prefix = f'\n\n## {label}\n'
        remaining = limit - estimate_tokens(out + prefix) - 2
        allowance = remaining // max(1, len(sections) - i)
        if allowance > 0:
            out += prefix + clip(body, allowance)
    return out


def handoff(output: str, limit: int = 700) -> str:
    # Preserve an actual structured summary if the model supplied one; otherwise
    # explicitly truncate. Never invent a semantic summary by clipping text.
    try:
        value = json.loads(output)
        if isinstance(value, dict) and isinstance(value.get('summary'), str):
            output = json.dumps({k: value[k] for k in ('summary', 'evidence', 'verification', 'risks', 'next_actions') if k in value}, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return clip(output, limit)
