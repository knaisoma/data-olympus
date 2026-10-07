# Data Olympus release planning

Status: active
Since: 2026-09-05

Target useful releases available every Monday. Select a coherent batch of
normally three to five open tickets before implementation, adjusted for effort,
risk, dependencies, and user benefit. Security remediation takes priority.
Explain progress under fixes, new capabilities, and improvements; these are
categories, not quotas. New ideas become issues before implementation.
Record acceptance criteria, dependencies, and validation for every selected issue.
Start feature branches from `release/new` and integrate reviewed PRs there.
Build each integration RC from content using `scripts/sdlc_version.py`; do not
commit the target version. Collect functional changes under `[Unreleased]`.
Select and validate the exact batch head, then squash it to `main` with the
generated `release: X.Y.Z` record under `.rules/release-routine.md`.

Unfinished work moves to a later batch and is preserved through the recut.
Hotfixes start at current stable main on `hotfix/new`, contain fixes only and
take priority in staging. Both paths share the promotion lock and review gates.
The agent holding operator authorization merges; reserved cases such as
migrations or destructive changes require explicit operator authorization.
A no-value cycle may end with No action. Never override failed gates to meet
the calendar. Until the first new-model release, the transitional preparation
and publication path in `.rules/release-routine.md` remains available.
