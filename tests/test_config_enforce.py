# tests/test_config_enforce.py
"""Config tests for enforcement settings."""
from __future__ import annotations

from data_olympus.config import load_config


def test_consult_ttl_default(monkeypatch) -> None:
    monkeypatch.delenv("KB_CONSULT_TTL_SEC", raising=False)
    cfg = load_config()
    assert cfg.consult_ttl_sec == 300


def test_consult_ttl_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_CONSULT_TTL_SEC", "120")
    cfg = load_config()
    assert cfg.consult_ttl_sec == 120


def test_governed_vocabulary_is_extensible_by_configuration(monkeypatch) -> None:  # noqa: ANN001
    """Issue #257: GOVERNED_KEYWORDS, GOVERNED_PATH_GLOBS and
    GOVERNED_COMMAND_PATTERNS shipped as module constants, and
    build_app_from_config never threaded a classifier, so an operator running
    the shipped entry point could not extend governed coverage without
    embedding the server in their own Python.

    The knobs EXTEND the shipped lists rather than replacing them, so enabling
    one cannot silently drop coverage the product already had.
    """
    from data_olympus.config import load_config
    from data_olympus.enforce_policy import (
        GOVERNED_KEYWORDS,
        classifier_from_config,
    )

    monkeypatch.setenv("KB_GOVERNED_EXTRA_KEYWORDS", "window test, input test")
    monkeypatch.setenv("KB_GOVERNED_EXTRA_PATH_GLOBS", "desktop/**")
    monkeypatch.setenv("KB_GOVERNED_EXTRA_COMMAND_PATTERNS", "xdotool")
    config = load_config()

    assert config.governed_extra_keywords == ("window test", "input test")
    assert config.governed_extra_path_globs == ("desktop/**",)
    assert config.governed_extra_command_patterns == ("xdotool",)

    classifier = classifier_from_config(config)
    # The operator's own action class is now governed...
    assert classifier.classify(intent="run a window test on the live desktop").is_governed_decision
    assert classifier.classify(action_path="desktop/session.md").is_governed_decision
    assert classifier.classify(action_diff="xdotool key Return").is_governed_decision
    # ...and every shipped keyword still is.
    for shipped in GOVERNED_KEYWORDS[:3]:
        assert classifier.classify(intent=f"we should {shipped} this").is_governed_decision


def test_no_extra_vocabulary_leaves_the_shipped_behaviour_untouched(monkeypatch) -> None:  # noqa: ANN001
    from data_olympus.config import load_config
    from data_olympus.enforce_policy import IntentClassifier, classifier_from_config

    for var in ("KB_GOVERNED_EXTRA_KEYWORDS", "KB_GOVERNED_EXTRA_PATH_GLOBS",
                "KB_GOVERNED_EXTRA_COMMAND_PATTERNS"):
        monkeypatch.delenv(var, raising=False)
    config = load_config()

    assert config.governed_extra_keywords == ()
    configured = classifier_from_config(config)
    shipped = IntentClassifier()
    probe = "update the architecture decision"
    assert (configured.classify(intent=probe).is_governed_decision
            == shipped.classify(intent=probe).is_governed_decision)


def test_the_shipped_entry_point_actually_uses_the_configured_vocabulary(
    tmp_path, monkeypatch,  # noqa: ANN001
) -> None:
    """The whole point of issue #257 part 2. IntentClassifier already took the
    three lists and ServerState already took a classifier, but
    build_app_from_config never threaded one, so an operator running
    `data-olympus-mcp` could not reach any of it. This asserts the wiring, not
    the classifier."""
    import subprocess

    from data_olympus.config import load_config
    from data_olympus.server import build_app_from_config

    kb = tmp_path / "kb"
    (kb / "universal" / "foundation").mkdir(parents=True)
    (kb / "universal" / "foundation" / "STD-U-001.md").write_text(
        "---\nid: STD-U-001\ntype: standard\nstatus: active\ntier: T1\n---\nbody\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=kb, check=True)
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_GOVERNED_EXTRA_KEYWORDS", "window test")

    app = build_app_from_config(load_config(), bootstrap_now=False)
    state = app._dolympus_state  # type: ignore[attr-defined]

    assert state.classifier.classify(
        intent="run a window test on the live desktop"
    ).is_governed_decision
