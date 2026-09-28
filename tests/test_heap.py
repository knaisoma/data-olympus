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

import contextlib
import platform
import queue
import subprocess
import sys
import threading
import time
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


def test_configure_pins_the_trim_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """glibc raises its trim threshold after large frees, so a worker thread's
    arena stops giving back its free top; a fixed threshold keeps it trimmed."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(heap, "_mallopt", lambda: lambda p, v: calls.append((p, v)) or 1)
    assert heap.configure_allocator() is True
    assert calls == [(heap.M_TRIM_THRESHOLD, heap.TRIM_THRESHOLD_BYTES)]


def test_configure_is_a_noop_without_mallopt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_mallopt", lambda: None)
    assert heap.configure_allocator() is False


def test_configure_survives_a_failing_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def vetoed() -> None:
        raise RuntimeError("ctypes.dlopen vetoed")

    monkeypatch.setattr(heap, "_mallopt", vetoed)
    assert heap.configure_allocator() is False


def test_configure_reports_a_rejected_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heap, "_mallopt", lambda: lambda *_args: 0)
    assert heap.configure_allocator() is False


def test_release_survives_a_failing_symbol_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading the allocator can fail with more than OSError (an audit hook can
    veto ctypes.dlopen with any exception); startup and refresh must not care."""
    def vetoed() -> None:
        raise RuntimeError("ctypes.dlopen vetoed")

    monkeypatch.setattr(heap, "_malloc_trim", vetoed)
    assert heap.release_free_heap() is False


@pytest.mark.skipif(not _GLIBC, reason="malloc_trim is a glibc function")
def test_release_uses_glibc_malloc_trim() -> None:
    assert heap._malloc_trim() is not None, "glibc exports malloc_trim; lookup found nothing"
    assert heap.release_free_heap() is True
    assert heap.configure_allocator() is True


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
    monkeypatch.setattr(server, "configure_allocator",
                        lambda: releases.append("configured") or True)
    monkeypatch.setattr(server, "release_free_heap",
                        lambda: releases.append("released") or True)
    server.build_app(kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
                     sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True)
    # Configured before the bootstrap build allocates, released after it.
    assert releases == ["configured", "released"]


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

    Each rebuild runs on a different long-lived thread, as the refresh executor
    and the request worker pool do in the server, over a corpus that makes the
    co-occurrence pair counter allocate and spill. Without the allocator
    configuration and the release step, every worker's arena keeps free memory
    resident and the total ends far above where it started.
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
    # What build_app does at startup, before any worker allocates.
    heap.configure_allocator()
    heap.release_free_heap()
    baseline = _rss_anon_kib()
    peak = baseline

    # Long-lived workers that are all alive at once, like a thread pool: glibc
    # gives each its own arena. A thread that exits hands its arena to the next
    # one, which would hide the retention this test exists to catch.
    workers = 4
    jobs: list[queue.Queue[object]] = [queue.Queue() for _ in range(workers)]
    done: queue.Queue[tuple[str, object]] = queue.Queue()

    def serve(inbox: queue.Queue[object]) -> None:
        while inbox.get() is not None:
            try:
                done.put(("ok", refresh_once(git=git, idx=idx, kb_main_path=tmp_git_kb)))
            except BaseException as exc:  # noqa: BLE001  reported to the test thread
                done.put(("error", repr(exc)))

    threads = [threading.Thread(target=serve, args=(q,), daemon=True) for q in jobs]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 240
    try:
        for cycle in range(workers):
            _push_doc(clone, f"DEC-{cycle}", " ".join(rnd.sample(vocab, 400)))
            jobs[cycle].put("rebuild")
            while True:
                try:
                    status, outcome = done.get(timeout=0.01)
                    break
                except queue.Empty:
                    peak = max(peak, _rss_anon_kib())
                    assert time.monotonic() < deadline, "rebuild workers did not finish"
            assert status == "ok", outcome
            assert isinstance(outcome, dict) and outcome["outcome"] == "rebuilt", outcome
        # Measured while every worker is still alive: a thread's exit changes
        # allocator state and would flatter the result.
        retained = _rss_anon_kib()
    finally:
        for q in jobs:
            q.put(None)
        for t in threads:
            t.join(30)
    assert not any(t.is_alive() for t in threads)

    # Fix-independent proof that the rebuilds ran the allocation-heavy path:
    # the co-occurrence pair counter populated its table. (The peak itself is
    # no guard: a pinned trim threshold lowers it as well.)
    import sqlite3
    with contextlib.closing(sqlite3.connect(tmp_path / "idx.db")) as conn:
        related = conn.execute("SELECT COUNT(*) FROM related_terms").fetchone()[0]
    assert related > 0, "co-occurrence expansion did not run; the test exercised nothing"

    retained_mib = (retained - baseline) / 1024
    peak_mib = (peak - baseline) / 1024
    # On glibc 2.41 this corpus leaves about 60 MiB resident across the four
    # live workers without the fix, and about 5 MiB with it.
    assert retained_mib < 10, (
        f"rebuilds kept {retained_mib:.0f} MiB resident (peak {peak_mib:.0f} MiB)"
    )
