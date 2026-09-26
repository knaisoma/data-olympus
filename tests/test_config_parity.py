"""Issue #291: every loaded setting must reach the running server.

`build_app` used to rebuild its `Config` from its own keyword arguments, so any
field it did not take as a parameter silently reverted to its default in
`state.config`. Four documented or env-read settings were affected in 0.10.0:
KB_CONSULT_TTL_SEC, KB_PENDING_CLAIM_TTL_SEC, KB_TRIGRAM_MODE and
KB_TRIGRAM_FALLBACK_THRESHOLD, and `http_port` was hardcoded to 8080.

Two checks, as the issue asks: a parity test that names the offending field for
every `Config` field, and an end-to-end check that anchors the parity to the
objects the server actually builds.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

import data_olympus.server as server
from data_olympus.config import Config, load_config

# Fields left at their loaded value rather than mutated, each for a stated
# reason. They are still compared for equality below; this list only says the
# test does not force a non-default value into them.
NOT_MUTATED = {
    "kb_main_path": "must point at the real test bundle",
    "kb_index_path": "must point at the real test index",
    "kb_remote_url": "a non-empty remote starts the git write pipeline",
    "embeddings_enabled": "enabling it loads an embedding model",
    # A structured value this test does not construct. Covered by the identity
    # assertion instead, which does not depend on mutation.
    "auth_principals": "structured; covered by the identity assertion",
    "status_weights": "structured; covered by the identity assertion",
    "kb_main_path_source": "an enum describing how the path was resolved",
    "kb_index_path_source": "an enum describing how the path was resolved",
}


def _bundle(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    (kb / "decisions").mkdir(parents=True)
    (kb / "decisions" / "D-1.md").write_text(
        "---\nid: D-1\ntype: decision\nstatus: active\ntier: meta\n"
        "title: Parity seed\n---\n# Seed\n\nParity seed document.\n",
        encoding="utf-8",
    )
    return kb


@pytest.fixture
def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("KB_MAIN_PATH", str(_bundle(tmp_path)))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")
    monkeypatch.setenv("KB_DISABLE_VERSION_CHECK", "1")
    return load_config()


def _mutate(cfg: Config, tmp_path: Path) -> Config:
    """Push every safely mutable field off its loaded value."""
    changes: dict[str, Any] = {}
    for field in dataclasses.fields(Config):
        name, value = field.name, getattr(cfg, field.name)
        if name in NOT_MUTATED:
            continue
        if isinstance(value, bool):
            # Relative to the class default, not the loaded value: a boolean the
            # environment already set away from its default would otherwise be
            # flipped back onto it, and a dropped field would pass unnoticed.
            changes[name] = not field.default
        elif isinstance(value, int):
            changes[name] = value + 7
        elif isinstance(value, float):
            changes[name] = round(value + 0.05, 4)
        elif value is None:
            changes[name] = 45  # review_due_after_days, an optional day count
        elif isinstance(value, tuple):
            changes[name] = (*value, f"parity-{name}")
        elif isinstance(value, list):
            changes[name] = [*value, f"parity-{name}"]
        elif isinstance(value, str | Path):
            is_path = "/" in str(value)
            changes[name] = str(tmp_path / f"parity-{name}") if is_path else f"parity-{name}"
    changes["tool_discovery_mode"] = "all" if cfg.tool_discovery_mode == "search" else "search"
    changes["kb_git_branch"] = "trunk"
    changes["maintenance_ledger_path"] = "tooling/parity-ledger.md"
    changes["write_block_tiers"] = ["T4"]
    changes["trusted_proxies"] = ["10.0.0.9"]
    changes["public_hostnames"] = ["parity.example"]
    changes["governed_extra_command_patterns"] = (r"^parity-cmd\b",)
    changes["embeddings_model"] = "parity/model"
    return dataclasses.replace(cfg, **changes)


def _build_and_capture(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    original = server.ServerState.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        original(self, *args, **kwargs)

    monkeypatch.setattr(server.ServerState, "__init__", spy)
    server.build_app_from_config(cfg, bootstrap_now=False)
    return captured


def test_every_config_field_reaches_the_running_state(
    loaded: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _mutate(loaded, tmp_path)
    mutated = [f.name for f in dataclasses.fields(Config)
               if getattr(cfg, f.name) != getattr(loaded, f.name)]
    # Guard the test itself: it must actually move most fields off default,
    # or equality below proves nothing about them.
    assert len(mutated) >= len(dataclasses.fields(Config)) - len(NOT_MUTATED) - 2, mutated

    running = _build_and_capture(cfg, monkeypatch)["config"]
    # Name the offending fields first, so a regression reports what was lost
    # rather than only that the object changed.
    dropped = {
        f.name: (getattr(cfg, f.name), getattr(running, f.name))
        for f in dataclasses.fields(Config)
        if getattr(running, f.name) != getattr(cfg, f.name)
    }
    assert not dropped, (
        "loaded settings did not reach state.config (configured, running): "
        f"{dropped}"
    )
    # The guarantee the fix makes: the production path hands the loaded object
    # through unchanged. Identity also covers the NOT_MUTATED fields, which the
    # comparison above cannot distinguish from their defaults.
    assert running is cfg


def test_not_mutated_list_names_only_real_fields() -> None:
    names = {f.name for f in dataclasses.fields(Config)}
    assert set(NOT_MUTATED) <= names, set(NOT_MUTATED) - names


def test_env_settings_reach_the_objects_the_server_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: environment, load_config, the production entry point."""
    monkeypatch.setenv("KB_MAIN_PATH", str(_bundle(tmp_path)))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")
    monkeypatch.setenv("KB_DISABLE_VERSION_CHECK", "1")
    monkeypatch.setenv("KB_CONSULT_TTL_SEC", "1234")
    monkeypatch.setenv("KB_PENDING_CLAIM_TTL_SEC", "4321")
    monkeypatch.setenv("KB_TRIGRAM_MODE", "on")
    monkeypatch.setenv("KB_TRIGRAM_FALLBACK_THRESHOLD", "7")

    captured = _build_and_capture(load_config(), monkeypatch)
    running, idx, ledger = captured["config"], captured["idx"], captured["ledger"]

    assert running.consult_ttl_sec == 1234
    assert running.pending_claim_ttl_sec == 4321
    assert running.trigram_fallback_enabled is True
    assert running.trigram_fallback_threshold == 7
    assert idx.trigram_fallback is True
    assert idx.trigram_fallback_threshold == 7
    # The ledger keeps retention private; it is the value the eviction uses.
    assert ledger._retention_sec == 1234.0
