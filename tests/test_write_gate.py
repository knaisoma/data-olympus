"""Unit tests for the write-integrity gates: CAS and content validation
(0.3.0 epic #72 scope items 3 and 4)."""
from __future__ import annotations

import hashlib
import os
import subprocess

from data_olympus.index import Index
from data_olympus.write_gate import (
    _blob_sha,
    check_cas,
    validate_postimage,
)


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


# ---- CAS (item 3) ----


def test_cas_noop_when_no_base_marker(tmp_path) -> None:
    """No base marker supplied -> CAS is a no-op (preserves pre-0.3.0 behavior)."""
    r = check_cas(worktree_path=str(tmp_path), target_path="x.md",
                  base_commit=None, base_blob_sha=None, target_file_hash=None)
    assert r.ok
    # A bare base_commit is advisory, not enforced.
    r2 = check_cas(worktree_path=str(tmp_path), target_path="x.md",
                   base_commit="HEAD", base_blob_sha=None, target_file_hash=None)
    assert r2.ok


def test_cas_blob_sha_matches_current_content(tmp_path) -> None:
    content = b"---\nid: STD\ntier: T1\n---\nbody\n"
    (tmp_path / "f.md").write_bytes(content)
    r = check_cas(worktree_path=str(tmp_path), target_path="f.md",
                  base_commit=None, base_blob_sha=_blob_sha(content),
                  target_file_hash=None)
    assert r.ok


def test_cas_blob_sha_mismatch_rejected(tmp_path) -> None:
    (tmp_path / "f.md").write_bytes(b"new content on disk\n")
    stale = _blob_sha(b"what the caller believed\n")
    r = check_cas(worktree_path=str(tmp_path), target_path="f.md",
                  base_commit=None, base_blob_sha=stale, target_file_hash=None)
    assert not r.ok
    assert "base_blob_sha mismatch" in r.reason


def test_cas_file_hash_mismatch_rejected(tmp_path) -> None:
    (tmp_path / "f.md").write_bytes(b"actual\n")
    stale_h = hashlib.sha256(b"expected\n").hexdigest()
    r = check_cas(worktree_path=str(tmp_path), target_path="f.md",
                  base_commit=None, base_blob_sha=None, target_file_hash=stale_h)
    assert not r.ok
    assert "target_file_hash mismatch" in r.reason


def test_cas_claiming_base_for_absent_file_rejected(tmp_path) -> None:
    """A caller that supplies a blob sha for a file that does not exist on the
    refreshed base is stale (the file was deleted or never existed)."""
    r = check_cas(worktree_path=str(tmp_path), target_path="missing.md",
                  base_commit=None, base_blob_sha=_blob_sha(b"x"),
                  target_file_hash=None)
    assert not r.ok


# ---- content validation gate (item 4) ----


def _index_with(tmp_path, files: dict[str, str]) -> Index:
    kb = tmp_path / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = kb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=kb, check=True,
                   env=_env())
    subprocess.run(["git", "add", "-A"], cwd=kb, check=True, env=_env())
    subprocess.run(["git", "commit", "--allow-empty", "-m", "seed"], cwd=kb,
                   check=True, env=_env())
    idx = Index(tmp_path / "index.db")
    idx.build(kb, source_commit="seed")
    return idx


def test_validate_accepts_wellformed_document(tmp_path) -> None:
    idx = _index_with(tmp_path, {})
    good = "---\nid: STD-U-050\ntype: standard\nstatus: active\ntier: T1\n---\n# body\n"
    r = validate_postimage(target_path="universal/foundation/STD-U-050.md",
                           postimage=good, idx=idx)
    assert r.ok


def test_validate_rejects_malformed_yaml(tmp_path) -> None:
    idx = _index_with(tmp_path, {})
    # Unterminated frontmatter block.
    bad = "---\nid: X\ntier: T1\n# no closing delimiter\nbody\n"
    r = validate_postimage(target_path="universal/foundation/X.md",
                           postimage=bad, idx=idx)
    assert not r.ok
    assert any(e["code"] == "invalid_frontmatter" for e in r.errors)


def test_validate_rejects_invalid_enum(tmp_path) -> None:
    idx = _index_with(tmp_path, {})
    bad = "---\nid: Y\ntype: nonsense\nstatus: active\ntier: T9\n---\nbody\n"
    r = validate_postimage(target_path="universal/foundation/Y.md",
                           postimage=bad, idx=idx)
    assert not r.ok
    codes = {e["code"] for e in r.errors}
    assert codes == {"invalid_enum"}
    fields = {e["field"] for e in r.errors}
    assert fields == {"type", "tier"}


def test_validate_rejects_duplicate_id_at_different_path(tmp_path) -> None:
    """A frontmatter id already used by a DIFFERENT path corrupts the next
    index rebuild (DuplicateIdError). Reject it (item 4)."""
    idx = _index_with(tmp_path, {
        "universal/foundation/STD-U-001.md":
            "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\nA\n",
    })
    forged = "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\nB\n"
    r = validate_postimage(
        target_path="decisions/DEC-forged.md", postimage=forged, idx=idx)
    assert not r.ok
    assert any(e["code"] == "duplicate_id" for e in r.errors)


def test_validate_allows_same_id_same_path_edit(tmp_path) -> None:
    """An id that resolves to the SAME path is a legitimate edit-in-place."""
    idx = _index_with(tmp_path, {
        "universal/foundation/STD-U-001.md":
            "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\nA\n",
    })
    edited = "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\nEDITED\n"
    r = validate_postimage(
        target_path="universal/foundation/STD-U-001.md", postimage=edited, idx=idx)
    assert r.ok


def test_validate_allows_memory_without_required_fields(tmp_path) -> None:
    """Memory-inbox documents legitimately carry no id/type/status/tier; the gate
    must not reject them just for being sparse (only actively malformed docs)."""
    idx = _index_with(tmp_path, {})
    mem = "---\ncreated_by: claude\ncreated_at: '2026-07-03T00:00:00+00:00'\n---\n\nnote\n"
    r = validate_postimage(target_path="memory/inbox/2026-07-03-note-abc123.md",
                           postimage=mem, idx=idx)
    assert r.ok


def test_validate_reserved_file_exempt_from_schema(tmp_path) -> None:
    idx = _index_with(tmp_path, {})
    # index.md is reserved; no frontmatter at all is fine.
    r = validate_postimage(target_path="decisions/index.md",
                           postimage="# Index\n\n- listing\n", idx=idx)
    assert r.ok


def test_rendered_memory_passes_validation() -> None:
    """The _render_memory output must pass the gate (cheap self-check, item 4)."""
    from data_olympus.tools_write import _render_memory
    out = _render_memory(text="a memory body", tags=["x", "y"],
                         agent_identity="claude")
    r = validate_postimage(target_path="memory/inbox/2026-07-03-a-memory-abc.md",
                           postimage=out, idx=None)
    assert r.ok
