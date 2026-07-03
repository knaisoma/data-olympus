"""Static checks on packaging metadata that the wizard + release depend on."""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text())


def test_setup_console_entry_present():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["data-olympus"] == "data_olympus.cli.main:main"


def test_wheel_force_includes_enforcement_machinery():
    """The wizard invokes bin/_kb_enforce.py + kb-enforce-hook at runtime, so the
    wheel MUST ship them under data_olympus/_bin/."""
    force = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    targets = set(force.values())
    assert "data_olympus/_bin/_kb_enforce.py" in targets
    assert "data_olympus/_bin/kb-enforce-hook" in targets
    assert "data_olympus/_bin/opencode/data-olympus-gate.ts" in targets
    # sources exist in the tree
    for src in force:
        assert (_ROOT / src).exists(), src


def test_license_and_metadata_declared():
    project = _pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    assert project["readme"] == "README.md"
    assert "Repository" in project["urls"]
    assert any("3.13" in c for c in project["classifiers"])


def test_sdist_includes_bin():
    include = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "bin" in include


def test_bin_root_resolves_to_shipped_or_repo():
    """setup_wizard.bin_root() must point at a dir that actually holds the
    enforce script (repo bin/ in the dev tree, packaged _bin/ once installed)."""
    from data_olympus import setup_wizard as w

    assert w.enforce_script().is_file()
