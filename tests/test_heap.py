"""Returning freed heap to the operating system after index work (issue #284).

An index rebuild allocates a large, short-lived working set (parsed corpus,
token sets, the co-occurrence pair counter) on a worker thread. glibc keeps
freed memory in that thread's arena rather than returning it, so a server that
rebuilt a few times on different threads held several build peaks of resident
memory for good and was eventually OOM-killed. The server now asks the
allocator to give free memory back after every refresh tick and after the
bootstrap build.
"""
from __future__ import annotations

import ctypes.util
import platform
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from data_olympus import heap
from data_olympus.git_ops import GitOps
from data_olympus.index import Index
from data_olympus.refresh import refresh_once

_GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}

_GLIBC = sys.platform.startswith("linux") and platform.libc_ver()[0] == "glibc"


def test_release_is_a_noop_where_the_allocator_cannot_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(heap, "_malloc_trim", lambda: None)
    assert heap.release_free_heap() is False


def test_release_asks_the_allocator_to_trim_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pads: list[int] = []
    monkeypatch.setattr(heap, "_malloc_trim", lambda: pads.append)
    assert heap.release_free_heap() is True
    assert pads == [0]


def test_release_survives_an_allocator_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_pad: int) -> int:
        raise OSError("trim failed")

    monkeypatch.setattr(heap, "_malloc_trim", lambda: boom)
    assert heap.release_free_heap() is False


@pytest.mark.skipif(not _GLIBC, reason="malloc_trim is a glibc function")
def test_release_uses_glibc_malloc_trim() -> None:
    assert ctypes.util.find_library("c") is not None
    assert heap.release_free_heap() is True


def _remote_for(kb: Path, tmp_path: Path) -> Path:
    """Give ``kb`` a bare origin and return a clone that can push to it."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(remote)],
                   check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(kb), "remote", "add", "origin", str(remote)],
                   check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(kb), "push", "-q", "-u", "origin", "main"],
                   check=True, env=_GIT_ENV)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, env=_GIT_ENV)
    return clone


def _push_doc(clone: Path, name: str, body: str) -> None:
    (clone / "decisions").mkdir(parents=True, exist_ok=True)
    (clone / "decisions" / f"{name}.md").write_text(f"---\nid: {name}\n---\n# {name}\n\n{body}\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", name], check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"],
                   check=True, env=_GIT_ENV)


def test_refresh_releases_heap_after_a_rebuild(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases: list[str] = []
    monkeypatch.setattr("data_olympus.refresh.release_free_heap",
                        lambda: releases.append("released") or True)
    git = GitOps(tmp_git_kb)
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit=git.head_sha())
    clone = _remote_for(tmp_git_kb, tmp_path)
    _push_doc(clone, "DEC-NEW", "A new decision.")

    result = refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)

    assert result["outcome"] == "rebuilt"
    assert releases == ["released"]


def test_refresh_releases_heap_on_a_quiet_tick(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request handlers allocate on their own threads between rebuilds; the
    periodic tick returns that memory too, not only a rebuild's."""
    releases: list[str] = []
    monkeypatch.setattr("data_olympus.refresh.release_free_heap",
                        lambda: releases.append("released") or True)
    git = GitOps(tmp_git_kb)
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit=git.head_sha())

    result = refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)

    assert result["outcome"] == "no_change"
    assert releases == ["released"]


def test_refresh_releases_heap_when_the_rebuild_fails(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases: list[str] = []
    monkeypatch.setattr("data_olympus.refresh.release_free_heap",
                        lambda: releases.append("released") or True)
    git = GitOps(tmp_git_kb)
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit=git.head_sha())
    clone = _remote_for(tmp_git_kb, tmp_path)
    # STD-U-001 already exists in the fixture corpus, so the rebuild refuses.
    (clone / "decisions").mkdir(parents=True, exist_ok=True)
    (clone / "decisions" / "DUP.md").write_text("---\nid: STD-U-001\n---\n# Duplicate\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "dup"], check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"],
                   check=True, env=_GIT_ENV)

    result = refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)

    assert result["outcome"] == "failed"
    assert releases == ["released"]


def test_bootstrap_build_releases_heap(
    tmp_kb: Path, tmp_index_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from data_olympus import server

    releases: list[str] = []
    monkeypatch.setattr(server, "release_free_heap",
                        lambda: releases.append("released") or True)
    server.build_app(kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
                     sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True)
    assert releases == ["released"]


def _rss_anon_kib() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("RssAnon:"):
            return int(line.split()[1])
    raise AssertionError("RssAnon missing from /proc/self/status")


@pytest.mark.skipif(not _GLIBC, reason="measures glibc arena retention through /proc")
def test_rebuilds_on_several_threads_do_not_keep_their_peaks(
    tmp_git_kb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression the issue describes, measured rather than mocked.

    Each rebuild runs on a different thread, as the refresh executor and the
    request worker pool do in the server, over a corpus large enough to make the
    co-occurrence pair counter allocate tens of mebibytes. Without the release
    step every thread's arena keeps its build peak and resident memory ends far
    above where it started.
    """
    import random

    # A low in-memory pair cap makes a small corpus spill the pair counter to
    # disk the way a large one does, which is the churn that fragments the heap.
    monkeypatch.setenv("KB_COOCCURRENCE_MAX_PAIRS", "100000")
    rnd = random.Random(284)
    vocab = [f"term{i:05d}" for i in range(6000)]
    docs = tmp_git_kb / "corpus"
    docs.mkdir()
    for n in range(80):
        body = " ".join(rnd.sample(vocab, 300))
        (docs / f"DOC-{n:03d}.md").write_text(f"---\nid: DOC-{n:03d}\n---\n# Doc {n}\n\n{body}\n")
    subprocess.run(["git", "-C", str(tmp_git_kb), "add", "-A"], check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(tmp_git_kb), "commit", "-q", "-m", "corpus"],
                   check=True, env=_GIT_ENV)
    git = GitOps(tmp_git_kb)
    idx = Index(tmp_path / "idx.db")
    idx.build(tmp_git_kb, source_commit=git.head_sha())
    clone = _remote_for(tmp_git_kb, tmp_path)
    heap.release_free_heap()
    baseline = _rss_anon_kib()
    peak = baseline

    # Long-lived workers that are all alive at once, like a thread pool: glibc
    # gives each its own arena. A thread that exits hands its arena to the next
    # one, which would hide the retention this test exists to catch.
    workers = 4
    jobs: list[queue.Queue[object]] = [queue.Queue() for _ in range(workers)]
    done: queue.Queue[dict[str, object]] = queue.Queue()

    def serve(inbox: queue.Queue[object]) -> None:
        while inbox.get() is not None:
            done.put(refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb))

    threads = [threading.Thread(target=serve, args=(q,), daemon=True) for q in jobs]
    for t in threads:
        t.start()
    try:
        for cycle in range(workers):
            _push_doc(clone, f"DEC-{cycle}", " ".join(rnd.sample(vocab, 400)))
            jobs[cycle].put("rebuild")
            while True:
                try:
                    outcome = done.get(timeout=0.01)
                    break
                except queue.Empty:
                    peak = max(peak, _rss_anon_kib())
            assert outcome["outcome"] == "rebuilt"
    finally:
        for q in jobs:
            q.put(None)
        for t in threads:
            t.join(30)

    retained_mib = (_rss_anon_kib() - baseline) / 1024
    peak_mib = (peak - baseline) / 1024
    assert peak_mib > 30, f"corpus too small to exercise the allocator: peak {peak_mib:.0f} MiB"
    assert retained_mib < 10, (
        f"rebuilds kept {retained_mib:.0f} MiB of a {peak_mib:.0f} MiB peak resident"
    )
