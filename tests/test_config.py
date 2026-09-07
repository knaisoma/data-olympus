"""Tests for config module."""
from pathlib import Path

import pytest

from data_olympus.config import (
    PATH_SOURCE_DEFAULT,
    PATH_SOURCE_ENV,
    Config,
    corpus_path_problem,
    load_config,
)


def _config_with_main_path(path: Path, source: str) -> Config:
    """A minimal Config carrying just the corpus path and its provenance."""
    return Config(
        kb_main_path=path,
        kb_index_path=Path("/index/kb.db"),
        kb_remote_url="",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        confidence_threshold=0.85,
        http_port=8080,
        kb_main_path_source=source,
    )


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults apply when env vars are unset."""
    for key in [
        "KB_MAIN_PATH",
        "KB_INDEX_PATH",
        "KB_REMOTE_URL",
        "KB_SYNC_INTERVAL_SEC",
        "KB_STALENESS_DEGRADED_SEC",
        "KB_CONFIDENCE_THRESHOLD",
        "KB_HTTP_PORT",
    ]:
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert cfg.kb_main_path == Path("/kb-main")
    assert cfg.kb_index_path == Path("/index/kb.db")
    assert cfg.kb_remote_url == ""
    assert cfg.sync_interval_sec == 60
    assert cfg.staleness_degraded_sec == 600
    assert cfg.confidence_threshold == 0.85
    assert cfg.http_port == 8080


def test_load_config_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env vars override defaults."""
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "main"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_SYNC_INTERVAL_SEC", "30")
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.9")
    monkeypatch.setenv("KB_HTTP_PORT", "9090")
    cfg = load_config()
    assert cfg.kb_main_path == tmp_path / "main"
    assert cfg.kb_index_path == tmp_path / "idx.db"
    assert cfg.sync_interval_sec == 30
    assert cfg.confidence_threshold == 0.9
    assert cfg.http_port == 9090


def test_load_config_rejects_bad_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold outside [0, 1] must raise."""
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "1.5")
    with pytest.raises(ValueError, match="KB_CONFIDENCE_THRESHOLD"):
        load_config()


def test_status_weights_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset KB_STATUS_WEIGHTS leaves the override empty (Index applies its
    built-in default map)."""
    monkeypatch.delenv("KB_STATUS_WEIGHTS", raising=False)
    cfg = load_config()
    assert cfg.status_weights is None


def test_status_weights_parsed_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_STATUS_WEIGHTS", '{"active": -3.0, "deprecated": 4.0}')
    cfg = load_config()
    assert cfg.status_weights == {"active": -3.0, "deprecated": 4.0}


def test_status_weights_rejects_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_STATUS_WEIGHTS", "not-json")
    with pytest.raises(ValueError, match="KB_STATUS_WEIGHTS"):
        load_config()


def test_status_weights_rejects_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_STATUS_WEIGHTS", '["active", -3.0]')
    with pytest.raises(ValueError, match="KB_STATUS_WEIGHTS"):
        load_config()


def test_config_is_frozen() -> None:
    """Config instances are immutable."""
    cfg = Config(
        kb_main_path=Path("/tmp/a"),
        kb_index_path=Path("/tmp/b.db"),
        kb_remote_url="git@example.com:x/y.git",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        confidence_threshold=0.85,
        http_port=8080,
    )
    with pytest.raises((AttributeError, TypeError)):
        cfg.http_port = 9090  # type: ignore[misc]


def test_config_loads_new_write_env_vars(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "kb"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "git@example.com:x/y.git")
    monkeypatch.setenv("KB_SYNC_INTERVAL_SEC", "60")
    monkeypatch.setenv("KB_STALENESS_DEGRADED_SEC", "600")
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.85")
    monkeypatch.setenv("KB_HTTP_PORT", "8080")
    monkeypatch.setenv("KB_WORKTREE_ROOT", "/wts")
    monkeypatch.setenv("KB_PENDING_ROOT", "/state/pending")
    monkeypatch.setenv("KB_PUSH_QUEUE_ROOT", "/state/push-queue")
    monkeypatch.setenv("KB_WRITE_BLOCK_TIERS", "T1,T2")
    monkeypatch.setenv("KB_WRITE_BLOCK_PATHS", "decisions/DEC-008-*.md")
    monkeypatch.setenv("KB_RATE_LIMIT_PER_HOUR", "50")
    monkeypatch.setenv("KB_GATE_CHECK_RATE_LIMIT_PER_HOUR", "7")
    monkeypatch.setenv("KB_PENDING_TIMEOUT_SEC", "86400")
    monkeypatch.setenv("KB_PENDING_QUEUE_CAP", "100")
    monkeypatch.setenv("KB_WORKTREE_IDLE_SEC", "1800")
    monkeypatch.setenv("KB_GIT_KEY_PATH", "/tmp/git-key")

    from data_olympus.config import load_config
    cfg = load_config()
    assert cfg.worktree_root == "/wts"
    assert cfg.pending_root == "/state/pending"
    assert cfg.push_queue_root == "/state/push-queue"
    assert cfg.write_block_tiers == ["T1", "T2"]
    assert cfg.write_block_paths == ["decisions/DEC-008-*.md"]
    assert cfg.rate_limit_per_hour == 50
    assert cfg.gate_check_rate_limit_per_hour == 7
    assert cfg.pending_timeout_sec == 86400
    assert cfg.pending_queue_cap == 100
    assert cfg.worktree_idle_sec == 1800
    assert cfg.git_key_path == "/tmp/git-key"


def test_config_defaults_for_new_write_vars(monkeypatch, tmp_path) -> None:
    """Empty defaults for the policy blocklist; sensible defaults for the rest."""
    for var in ["KB_WORKTREE_ROOT", "KB_PENDING_ROOT", "KB_PUSH_QUEUE_ROOT",
                "KB_WRITE_BLOCK_TIERS", "KB_WRITE_BLOCK_PATHS", "KB_RATE_LIMIT_PER_HOUR",
                "KB_GATE_CHECK_RATE_LIMIT_PER_HOUR",
                "KB_PENDING_TIMEOUT_SEC", "KB_PENDING_QUEUE_CAP", "KB_WORKTREE_IDLE_SEC",
                "KB_GIT_KEY_PATH"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "kb"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")

    from data_olympus.config import load_config
    cfg = load_config()
    assert cfg.worktree_root == "/kb-worktrees"
    assert cfg.pending_root == "/state/pending"
    assert cfg.push_queue_root == "/state/push-queue"
    assert cfg.write_block_tiers == []
    assert cfg.write_block_paths == []
    assert cfg.rate_limit_per_hour == 100
    assert cfg.gate_check_rate_limit_per_hour == 0
    assert cfg.pending_timeout_sec == 86400
    assert cfg.pending_queue_cap == 100
    assert cfg.worktree_idle_sec == 3600
    assert cfg.git_key_path == "/tmp/git-key"


def test_config_loads_audit_log_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "kb"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")
    monkeypatch.setenv("KB_AUDIT_LOG_PATH", "/custom/audit.log")
    from data_olympus.config import load_config
    cfg = load_config()
    assert cfg.audit_log_path == "/custom/audit.log"


# --- Issue #244: a blank path setting must not silently index the cwd ---------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  "])
def test_blank_kb_main_path_is_unset_not_cwd(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """A set-but-blank KB_MAIN_PATH applies the documented default.

    ``os.environ.get(name, default)`` only falls back when the variable is
    ABSENT, so a blank value (what an unsubstituted compose/Helm/CI variable
    produces) used to reach ``Path("")``, which is ``Path(".")`` -- the server
    silently indexed its own working directory.
    """
    monkeypatch.setenv("KB_MAIN_PATH", blank)
    cfg = load_config()
    assert cfg.kb_main_path == Path("/kb-main")
    assert cfg.kb_main_path != Path(".")
    assert cfg.kb_main_path_source == PATH_SOURCE_DEFAULT


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  "])
def test_blank_kb_index_path_is_unset_not_cwd(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Same rule for KB_INDEX_PATH; the issue names both path settings."""
    monkeypatch.setenv("KB_INDEX_PATH", blank)
    cfg = load_config()
    assert cfg.kb_index_path == Path("/index/kb.db")
    assert cfg.kb_index_path != Path(".")
    assert cfg.kb_index_path_source == PATH_SOURCE_DEFAULT


def test_supplied_path_settings_are_honoured_and_marked_as_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real value still wins, and is recorded as operator-supplied."""
    monkeypatch.setenv("KB_MAIN_PATH", "/srv/kb")
    monkeypatch.setenv("KB_INDEX_PATH", "/srv/index/kb.db")
    cfg = load_config()
    assert cfg.kb_main_path == Path("/srv/kb")
    assert cfg.kb_index_path == Path("/srv/index/kb.db")
    assert cfg.kb_main_path_source == PATH_SOURCE_ENV
    assert cfg.kb_index_path_source == PATH_SOURCE_ENV


def test_a_nonblank_path_setting_is_used_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only BLANK means unset. A non-blank value is never rewritten.

    A path may legitimately carry leading or trailing whitespace, so silently
    trimming it would be a different bug from the one #244 fixes. A padded path
    that does not exist fails loudly through corpus_path_problem instead.
    """
    monkeypatch.setenv("KB_MAIN_PATH", "  /srv/kb  ")
    cfg = load_config()
    assert cfg.kb_main_path == Path("  /srv/kb  ")
    assert cfg.kb_main_path_source == PATH_SOURCE_ENV
    msg = corpus_path_problem(cfg)
    assert msg is not None and "  /srv/kb  " in msg


def test_corpus_problem_is_none_for_a_real_directory(tmp_path: Path) -> None:
    """An existing directory is not a problem -- including an EMPTY one.

    The empty-corpus startup path is supported behaviour (see
    tests/test_server_smoke.py::test_empty_corpus_startup_builds_schema_and_read_tools_degrade);
    this fix targets missing/blank configuration, not an empty corpus.
    """
    empty = tmp_path / "empty-kb"
    empty.mkdir()
    cfg = _config_with_main_path(empty, PATH_SOURCE_ENV)
    assert corpus_path_problem(cfg) is None


def test_corpus_problem_names_setting_path_and_env_origin(tmp_path: Path) -> None:
    """An operator-supplied path that is missing names all three facts."""
    missing = tmp_path / "nope"
    cfg = _config_with_main_path(missing, PATH_SOURCE_ENV)
    msg = corpus_path_problem(cfg)
    assert msg is not None
    assert "KB_MAIN_PATH" in msg
    assert str(missing) in msg
    assert "environment variable" in msg


def test_corpus_problem_says_the_path_came_from_the_default(tmp_path: Path) -> None:
    """The reported symptom: the default named, but never configured by anyone.

    The old message was ``KB root not a directory: /kb-main`` -- a path the
    operator had not set and no setting name to search for.

    The path is a missing directory under ``tmp_path`` rather than the literal
    ``/kb-main``, because that default DOES exist inside the product's own
    container image; asserting on it directly would make this test pass or fail
    depending on where it runs. That the built-in default is ``/kb-main`` is
    pinned separately by :func:`test_load_config_defaults`.
    """
    missing_default = tmp_path / "kb-main"
    cfg = _config_with_main_path(missing_default, PATH_SOURCE_DEFAULT)
    msg = corpus_path_problem(cfg)
    assert msg is not None
    assert "KB_MAIN_PATH" in msg
    assert str(missing_default) in msg
    assert "unset or blank" in msg
    assert "built-in" in msg


def test_corpus_problem_distinguishes_a_file_from_a_missing_path(
    tmp_path: Path,
) -> None:
    """A path that exists but is a file is a different mistake from an absent one."""
    a_file = tmp_path / "kb.txt"
    a_file.write_text("not a corpus")
    msg = corpus_path_problem(_config_with_main_path(a_file, PATH_SOURCE_ENV))
    assert msg is not None and "is not a directory" in msg
    gone = corpus_path_problem(
        _config_with_main_path(tmp_path / "absent", PATH_SOURCE_ENV)
    )
    assert gone is not None and "does not exist" in gone


def test_bootstrap_refuses_a_missing_corpus_with_the_actionable_message(
    tmp_path: Path,
) -> None:
    """build_app_from_config is the production bootstrap path (main() uses it)."""
    from data_olympus import server

    cfg = _config_with_main_path(tmp_path / "absent", PATH_SOURCE_DEFAULT)
    with pytest.raises(NotADirectoryError) as excinfo:
        server.build_app_from_config(cfg, bootstrap_now=True)
    assert "KB_MAIN_PATH" in str(excinfo.value)
