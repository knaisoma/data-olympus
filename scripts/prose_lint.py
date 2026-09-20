"""Mechanical gate for the project's writing rules.

Vendored copy. The canonical source is `tooling/prose_lint.py` in the operator's
company-knowledge repository, where the rules and this file are maintained;
changes belong there first and are copied here, so the two cannot drift by
someone editing only the downstream copy.

Usage:
    python3 scripts/prose_lint.py <path> [<path> ...]

A <path> may be a file or a directory; directories are searched for *.md and
*.mdx. Exits 0 when clean, 1 on a finding, 2 on a usage error. Findings print as
`path:line: rule: excerpt`.

Rules sit in two tiers because the two kinds of failure have opposite costs.

Tier one (RAW_RULES) is checked on raw lines with no Markdown interpretation at
all. A bypass here defeats the gate's whole purpose, so it is given nothing to
parse. Four rounds of companion review found a bypass in the Markdown masking
layer every time, and measurement settled the question: across 391 authored
files the masking suppressed 7 em-dashes out of 201, 5 of them this
repository's own documentation of the rule.

Tier two (LINE_RULES, PARA_RULES) is checked against Markdown-masked prose,
where a false positive is the expensive failure and a miss is a nit. Its
recognition is one ordered state machine rather than layered passes, and it
covers a practical subset, not CommonMark.

Limits that apply to both tiers: matching is within a single line (PARA_RULES
within a single paragraph), so a pattern split across a line break is not
detected; and the allow marker is textual, so a line genuinely ending in it is
exempted whether or not that was the intent.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CURLY = {'’': "'", '‘': "'", '“': '"', '”': '"'}


def normalise(text: str) -> str:
    """Fold typographic quotes so a rule cannot be evaded by autocorrect."""
    for a, b in CURLY.items():
        text = text.replace(a, b)
    return text


# Tier 1. Checked on RAW lines, with no Markdown interpretation at all.
# These are the rules the gate exists for, and a bypass in them is a failure of
# its whole promise, so they are given nothing to parse and therefore nothing to
# get wrong. Measured over 391 authored files, Markdown masking suppressed 7
# em-dashes out of 201, and 5 of those were this repository's own documentation
# of the rule. That is not worth a parser's bypass surface. The few genuine
# cases, a banned character inside a command or a quotation, use the allow
# marker, which is one unambiguous trailing token rather than a lexical state.
RAW_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ('em-dash', re.compile(r'—'),
     'em-dash: use a comma, parentheses, a colon, or two sentences'),
    ('en-dash-as-em', re.compile(r'(?<=\s)–(?=\s)'),
     'en-dash used as a sentence dash'),
    ('ai-authorship', re.compile(
        r'(?i)\bgenerated (?:with|by)\s+\[?(?:claude|codex|chatgpt|gpt|copilot|gemini'
        r'|cursor|an? (?:ai|llm))'
        r'|co-authored-by:\s*(?:claude|codex|chatgpt|gpt|copilot|gemini|cursor|openai|anthropic)'
        r'|\U0001F916'
        r'|\bas an ai\b'
        r'|\bwritten by (?:claude|codex|chatgpt|an ai)\b'),
     'agent authorship credit (STD-U-510)'),
]

# Tier 2. Checked on Markdown-masked prose, because here a false positive is the
# expensive failure and a miss is a missed stylistic nit, not a breach.
LINE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ('not-just', re.compile(
        r"(?i)\b(?:is|are|was|were|it's|that's)\s+not\s+just\b|\bmore than just\b"),
     '"not just" escalation'),
    ('rhetorical-frag', re.compile(
        r"(?i)here's the thing|\bthe (?:result|catch|problem|kicker|twist)\?|but here's"),
     'rhetorical fragment'),
    ('llm-diction', re.compile(
        r'(?i)\b(?:delve|tapestry|testament to|in the realm of|navigate the landscape'
        r'|seamlessly|a seamless)\b'),
     'LLM diction'),
]

# Spans soft line breaks so ordinary wrapping cannot defeat it. The subject must
# repeat and be a contraction, and the second clause must assert rather than
# negate again: "It's not X. It's Y." is the rhetorical shape, while
# "This is not X. This is Y." and "It's not X. It's not Y." are ordinary prose.
# Known limitation: emphasis markers between the clauses defeat it.
PARA_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ('neg-then-pos', re.compile(
        r"(?i)\b(it's|that's)\s+not\s+\S[^.;!?]{0,60}[.;!?]\s+\1(?!\s+not\b)\b"),
     'two-beat "not X. It\'s Y." reveal'),
]

FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})(.*)$')
LINK_DEST = re.compile(r'(?<!\\)\[[^\]]*\]\((<[^>]*>|[^()\s]*)\)')
AUTOLINK = re.compile(r'(?<!\\)<(?:[a-zA-Z][a-zA-Z0-9+.-]*://[^>\s]*|[^>\s@]+@[^>\s]+)>')
ALLOW = re.compile(r'(?<!\\)<!--\s*prose-lint:\s*allow\s*-->\s*$')

BLANK = ' '
# Stands in for a masked span. It is not whitespace, so a masked operand still
# reads as content to a rule, and it matches no rule itself.
FILLER = '\x01'
DELIMITERS = set('`[]()<>')
PARA_BREAK = (0, '')


def strip_escapes(line: str) -> str:
    """Neutralise a backslash escape without erasing what it escapes.

    A backslashed em-dash stays visible prose; a backslashed delimiter loses
    only its delimiter role.
    """
    out: list[str] = []
    i = 0
    while i < len(line):
        if line[i] == '\\' and i + 1 < len(line):
            nxt = line[i + 1]
            out.append(BLANK)
            out.append(BLANK if nxt in DELIMITERS else nxt)
            i += 2
            continue
        out.append(line[i])
        i += 1
    return ''.join(out)


def mask_code_spans(line: str) -> str:
    """Replace inline code spans, delimiters included, with FILLER.

    Runs before any other inline recognition, so a comment opener or a link
    written inside code is literal text and cannot change the scanner's state.
    """
    out = list(line)
    i, n = 0, len(line)
    while i < n:
        if line[i] != '`':
            i += 1
            continue
        run = 0
        while i + run < n and line[i + run] == '`':
            run += 1
        close = line.find('`' * run, i + run)
        while close != -1 and close + run < n and line[close + run] == '`':
            close = line.find('`' * run, close + run + 1)
        if close == -1:
            i += run
            continue
        for j in range(i, close + run):
            out[j] = FILLER
        i = close + run
    return ''.join(out)


def opens_fence(marker: str, info: str) -> bool:
    """A backtick fence's info string may not contain a backtick."""
    return marker[0] != '`' or '`' not in info


def closes_fence(marker: str, info: str, char: str, run: int) -> bool:
    return marker[0] == char and len(marker) >= run and not info.strip()


def prose_lines(text: str) -> list[tuple[int, str]]:
    """Return (line number, masked prose) for every checkable line.

    Per line: a fenced block wins over everything; inside a comment only its
    terminator is recognised; otherwise code spans are masked first, then
    comments, then links and autolinks. Every dropped line emits a paragraph
    break, so sentences either side of a code block are never joined.
    """
    out: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for n, raw in enumerate(normalise(text).splitlines(), 1):
        if fence is not None:
            m = FENCE.match(raw)
            if m and closes_fence(m.group(1), m.group(2), *fence):
                fence = None
            out.append(PARA_BREAK)
            continue
        if in_comment:
            close = raw.find('-->')
            if close == -1:
                out.append(PARA_BREAK)
                continue
            in_comment = False
            raw = BLANK * (close + 3) + raw[close + 3:]
        masked = mask_code_spans(strip_escapes(raw))
        m = FENCE.match(masked)
        if m and FILLER not in m.group(0) and opens_fence(m.group(1), m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            out.append(PARA_BREAK)
            continue
        while True:
            opened = masked.find('<!--')
            if opened == -1:
                break
            close = masked.find('-->', opened + 4)
            if close != -1:
                masked = masked[:opened] + BLANK * (close + 3 - opened) + masked[close + 3:]
                continue
            masked = masked[:opened]
            in_comment = True
            break
        if ALLOW.search(raw):
            out.append(PARA_BREAK)
            continue
        masked = LINK_DEST.sub(
            lambda mo: mo.group(0)[:mo.start(1) - mo.start(0)]
            + FILLER * len(mo.group(1))
            + mo.group(0)[mo.end(1) - mo.start(0):], masked)
        masked = AUTOLINK.sub(lambda mo: FILLER * len(mo.group(0)), masked)
        out.append((n, masked))
    return out


def raw_lines(text: str) -> list[tuple[int, str]]:
    """Every line, uninterpreted, minus those carrying an allow marker."""
    return [(n, ln) for n, ln in enumerate(normalise(text).splitlines(), 1)
            if not ALLOW.search(ln)]


def lint_text(text: str) -> list[tuple[int, str, str, str]]:
    findings: list[tuple[int, str, str, str]] = []
    for n, line in raw_lines(text):
        for name, rx, why in RAW_RULES:
            for m in rx.finditer(line):
                start = max(0, m.start() - 30)
                findings.append((n, name, why, line[start:m.end() + 30].strip()))
    lines = prose_lines(text)
    for n, line in lines:
        if not n:
            continue
        for name, rx, why in LINE_RULES:
            for m in rx.finditer(line):
                start = max(0, m.start() - 30)
                excerpt = line[start:m.end() + 30].strip().replace(FILLER, '`')
                findings.append((n, name, why, excerpt))
    para: list[tuple[int, str]] = []
    for item in lines + [PARA_BREAK]:
        if item[0] and item[1].strip():
            para.append(item)
            continue
        if para:
            joined = re.sub(FILLER + '+', FILLER, ' '.join(t for _, t in para))
            for name, rx, why in PARA_RULES:
                for m in rx.finditer(joined):
                    findings.append((para[0][0], name, why,
                                     m.group(0).strip().replace(FILLER, '`')))
            para = []
    findings.sort(key=lambda f: (f[0], f[1]))
    return findings


def ignored(root: Path) -> list[str]:
    f = root / '.prose-lintignore'
    if not f.exists():
        return []
    return [ln.strip() for ln in f.read_text().splitlines()
            if ln.strip() and not ln.startswith('#')]


def repo_root(start: Path) -> Path:
    for parent in [start.resolve()] + list(start.resolve().parents):
        if (parent / '.prose-lintignore').exists() or (parent / '.git').exists():
            return parent
    return start.resolve()


def lexical_rel(path: Path, root: Path) -> str | None:
    """Repository-relative path without following working-tree symlinks."""
    base = path if path.is_absolute() else Path.cwd() / path
    parts: list[str] = []
    for part in base.parts[1:]:
        if part == '..':
            if parts:
                parts.pop()
        elif part not in ('.', ''):
            parts.append(part)
    absolute = Path(base.parts[0]).joinpath(*parts)
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        return None


def skip(path: Path, root: Path, rules: list[str], lexical: bool = False) -> bool:
    """A rule ending in `/` is a directory prefix; anything else is an exact path."""
    rel = lexical_rel(path, root) if lexical else None
    if rel is None:
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            return False
    for r in rules:
        if r.endswith('/'):
            if rel.startswith(r):
                return True
        elif rel == r:
            return True
    return False


def targets(paths: list[str], require_disk: bool = True):
    for p in paths:
        path = Path(p)
        if not require_disk:
            yield path
            continue
        if path.is_dir():
            for suffix in ('*.md', '*.mdx'):
                yield from sorted(path.rglob(suffix))
        elif path.exists() or not require_disk:
            yield path
        else:
            print(f'prose-lint: no such path: {p}', file=sys.stderr)
            raise SystemExit(2)


def staged_bytes(path: Path, root: Path) -> str:
    """The bytes git will commit. Exits 2 rather than falling back to disk."""
    rel = lexical_rel(path, root)
    if rel is None:
        print(f'prose-lint: {path} is outside {root}; cannot read it from the index',
              file=sys.stderr)
        raise SystemExit(2)
    try:
        out = subprocess.run(['git', '-C', str(root), 'show', f':{rel}'],
                             capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f'prose-lint: {rel} has no staged content; refusing to read the working tree',
              file=sys.stderr)
        raise SystemExit(2) from exc
    return out.stdout.decode('utf-8', errors='replace')


def main(argv: list[str]) -> int:
    from_index = '--from-index' in argv
    argv = [a for a in argv if a != '--from-index']
    if not argv:
        print('prose-lint: no paths given', file=sys.stderr)
        return 2
    total = 0
    counts: dict[str, int] = {}
    root = repo_root(Path(argv[0]))
    rules = ignored(root)
    for path in targets(argv, require_disk=not from_index):
        if skip(path, root, rules, lexical=from_index):
            continue
        if from_index:
            text = staged_bytes(path, root)
        else:
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError) as exc:
                print(f'prose-lint: cannot read {path}: {exc}', file=sys.stderr)
                return 2
        for n, name, why, excerpt in lint_text(text):
            print(f'{path}:{n}: {name}: {why}')
            print(f'    {excerpt}')
            counts[name] = counts.get(name, 0) + 1
            total += 1
    if total:
        summary = ', '.join(f'{k}={v}' for k, v in sorted(counts.items()))
        print(f'\nprose-lint: {total} finding(s): {summary}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
