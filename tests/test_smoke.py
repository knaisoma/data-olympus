import tomllib
from pathlib import Path

import data_olympus


def test_package_imports_and_exposes_version():
    # __version__ is read from the installed distribution metadata, which is the
    # pyproject [project].version the release chain tags on. Assert they agree
    # rather than pinning a literal that would silently drift each release.
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    assert data_olympus.__version__ == pyproject["project"]["version"]
