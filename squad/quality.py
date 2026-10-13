"""Compact writing policy and explainable handoff checks; no AI-authorship score."""
from __future__ import annotations

import json
import re
from importlib.resources import files

SOURCE = 'https://github.com/petergyang/no-ai-slop'
REVISION = '000650b156983f5159695b441477f4e63b25dc85'

POLICY = '''Quality policy (adapted from Peter Yang's no-ai-slop):
Preserve the user's meaning, voice and specific facts. Use direct verbs and concrete
details. Remove generic openers, inflated claims, binary contrast slogans,
faux insights, decorative formatting and recap endings. Keep useful uncertainty.
Name sources; never invent evidence, measurements, quotations or completed work.
Apply prose edits only to prose, preserving code, identifiers, logs and quotations.
For task results return JSON: summary (nonempty string), evidence (list of concrete
observations or artifact references), verification (list of checks and actual
outcomes, or an explicit reason verification could not run), risks (list),
next_actions (list). Empty risks/next_actions are fine when there are none.
Separate plans from work performed. Disclose stubs, skipped checks and blockers.
Review the result before submitting. Writing checks cannot establish correctness;
independent review must compare evidence with the user's acceptance criteria.
Full upstream rules/eval: squad quality policy --full, or squad://quality/no-ai-slop.
'''


def policy(config=None):
    return '' if (config or {}).get('quality', {}).get('mode') == 'off' else POLICY


def upstream(name='SKILL.md'):
    if name not in {'SKILL.md', 'eval.md', 'LICENSE'}:
        raise ValueError('Unknown policy document')
    return files('squad').joinpath('vendor', 'no_ai_slop', name).read_text(encoding='utf-8')


PATTERNS = [
    ('generic opener', r"\b(here['’]s the thing|let me be clear|the uncomfortable truth is)\b"),
    ('faux insight', r'\b(what nobody tells you|the part everyone misses|what most people get wrong)\b'),
    ('inflated claim', r'\b(game changer|paradigm shift|cutting-edge|marks a pivotal moment|a testament to)\b'),
    ('vague attribution', r'\b(experts agree|studies show|widely regarded as)\b'),
    ('filler', r"\b(it['’]s worth noting|it['’]s important to note|at the end of the day|let['’]s dive in)\b"),
    ('stock wording', r'\b(delve|foster|leverage|utilize|supercharge|ever-evolving)\b'),
]


def inspect(text: str, handoff: bool = False) -> dict:
    if len(text) > 2_000_000:
        raise ValueError('Quality input exceeds 2 MB of text')
    errors, findings = [], []
    prose = text
    if handoff:
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError()
        except (ValueError, TypeError):
            value = {}
            errors.append('Return a JSON object with summary, evidence, verification, risks, next_actions')
        if not isinstance(value.get('summary'), str) or not value.get('summary', '').strip():
            errors.append('Missing nonempty summary')
        for field in ('evidence', 'verification', 'risks', 'next_actions'):
            entries = value.get(field)
            if not isinstance(entries, list) or any(not isinstance(x, str) or not x.strip() for x in entries):
                errors.append(f'{field} must be a list of nonempty strings')
            elif field in {'evidence', 'verification'} and not entries:
                errors.append(f'{field} must describe observations/checks or a specific blocker')
        prose = value.get('summary', '') if isinstance(value.get('summary'), str) else ''
    # Ignore fenced code, inline code, and quoted example lines. This is a small
    # advisory subset; the vendored upstream guide covers semantic patterns too.
    prose = re.sub(r'```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`', '', prose)
    for number, line in enumerate(prose.splitlines(), 1):
        if line.lstrip().startswith('>'):
            continue
        for label, pattern in PATTERNS:
            if re.search(pattern, line, re.I):
                findings.append({'pattern': label, 'line': number, 'excerpt': line[:300],
                                 'fix': 'Use a specific fact or direct wording; preserve intentional quotations.'})
    return {'passed': not errors, 'errors': errors, 'writing_findings': findings,
            'source': SOURCE, 'revision': REVISION,
            'scope': 'Handoff completeness and advisory wording checks; does not verify truth or code correctness.'}


def evaluate(text: str, config: dict, handoff: bool = True) -> dict:
    mode = config.get('quality', {}).get('mode', 'warn')
    if mode not in {'off', 'warn', 'strict'}:
        raise ValueError('quality.mode must be off, warn or strict')
    result = inspect(text, handoff=handoff) if mode != 'off' else {'passed': True, 'errors': [], 'writing_findings': []}
    return {**result, 'mode': mode, 'blocked': mode == 'strict' and not result['passed']}
