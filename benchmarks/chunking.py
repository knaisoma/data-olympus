"""Whitespace-token windowed chunking.

Chunk boundaries are on whitespace tokens (not BPE tokens) so chunking is
dependency-free and deterministic. `size`/`overlap` are in whitespace tokens.
"""
from __future__ import annotations


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    if overlap >= size:
        raise ValueError(f"overlap ({overlap}) must be < size ({size})")
    words = text.split()
    if not words:
        return []
    step = size - overlap
    chunks: list[str] = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + size]))
        i += step
    return chunks
