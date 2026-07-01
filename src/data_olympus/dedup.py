"""Deterministic repo<->KB overlap detection. No LLM: exact-hash for duplicates,
heading-set + k-shingle Jaccard for partial overlap. Ambiguous partials are left
for the calling agent to judge (escalation path)."""
from __future__ import annotations

import hashlib
import re

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$", re.MULTILINE)
_WS_RE = re.compile(r"\s+")


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def normalize_markdown(text: str) -> str:
    body = strip_frontmatter(text)
    return _WS_RE.sub(" ", body).strip().lower()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_markdown(text).encode("utf-8")).hexdigest()


def extract_headings(text: str) -> set[str]:
    return {m.strip().lower() for m in _HEADING_RE.findall(text)}


def shingles(text: str, k: int = 5) -> set[str]:
    words = normalize_markdown(text).split()
    if not words:
        return set()
    if len(words) < k:
        return {" ".join(words)}
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def classify_overlap(
    local_text: str, kb_text: str, *, jaccard_threshold: float = 0.6,
) -> tuple[str, list[str]]:
    """Return (classification, overlap_headings)."""
    if content_hash(local_text) == content_hash(kb_text):
        return ("imported_duplicate", [])
    if jaccard(shingles(local_text), shingles(kb_text)) >= jaccard_threshold:
        shared = sorted(extract_headings(local_text) & extract_headings(kb_text))
        return ("partial_overlap", shared)
    return ("unique", [])
