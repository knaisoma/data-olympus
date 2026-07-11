"""Update-available detection (issue #146, first slice).

Pure version comparison used to tell an operator/model that a newer
`data-olympus` release has been published. The network lookup of the latest
version already exists in ``setup_wizard.latest_version()``; this module is the
decision logic that turns (installed, latest) into an "update available" signal,
plus a human-facing one-line notice. Wiring it into a runtime surface (health
response / MCP annotation) is the remainder of #146.
"""
from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


def _parse(version: str | None) -> tuple[int, int, int] | None:
    """Parse the leading ``X.Y.Z`` (optional ``v`` prefix; ignores any pre-release
    or build suffix), or None if it is missing/unparseable."""
    if not version:
        return None
    m = _VERSION_RE.match(version)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def update_available(installed: str, latest: str | None) -> bool:
    """True when ``latest`` is a strictly newer release than ``installed``.

    Fail-safe: an unknown/unparseable ``latest`` (e.g. offline lookup returned
    None) or an unparseable ``installed`` returns False, so a failed version
    check never nags the operator with a bogus "update available".
    """
    cur = _parse(installed)
    new = _parse(latest)
    if cur is None or new is None:
        return False
    return new > cur


def update_notice(installed: str, latest: str | None) -> str | None:
    """A one-line "new version available" notice, or None when up to date or the
    latest is unknown. Suitable for a log line, a health field, or a model-facing
    annotation."""
    if not update_available(installed, latest):
        return None
    return (
        f"A newer data-olympus version is available: {latest} "
        f"(installed {installed}). Update at your convenience."
    )
