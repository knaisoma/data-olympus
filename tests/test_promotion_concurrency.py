"""All delivered privileged SDLC workflows serialize on one product lock.

Admitted runs take the shared ``data-olympus-promotion`` group. A run that its
jobs would refuse gets a private per-run group, so it can never cancel a
pending publication, promotion or recut (GitHub keeps one pending run per
group). The one exception is ``set-channel.yml``: it has no ref guard and acts
from any ref, so it takes the bare lock unconditionally. ``tag-release.yml``
is admitted from ``main`` only; a malformed ``candidate_tag`` cannot be
expressed in a group, so such a run still enters the lock. The group
expressions are evaluated here, not compared as text.
"""
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
LOCK = "data-olympus-promotion"
REPOSITORY = "knaisoma/data-olympus"

# A missing or renamed file must fail, never skip.
DELIVERED = (
    "recut-release-branch.yml", "hotfix-cut.yml", "rc-publish-stage.yml", "promote-release.yml",
    "set-channel.yml", "tag-release.yml",
)
# No ref guard and real work from any ref: the group is the bare lock, never a
# conditional with a run-scoped fallback.
UNCONDITIONAL = ("set-channel.yml",)
# Every lock member has landed. Add a new one to DELIVERED and ADMISSION below.
PENDING: dict[str, str] = {}  # keep: a new lock member starts here

_DISPATCH = {"github.ref": "refs/heads/main", "vars.SDLC_PIPELINE": "enabled"}
_RUN = "github.event.workflow_run."
_STAGE = {
    "vars.SDLC_PIPELINE": "enabled", "github.repository": REPOSITORY,
    _RUN + "conclusion": "success", _RUN + "event": "push",
    _RUN + "head_branch": "release/new",
    _RUN + "head_repository.full_name": REPOSITORY,
}
# Per workflow: admitted contexts, and contexts its jobs refuse.
ADMISSION = {
    "recut-release-branch.yml": ([_DISPATCH], [
        {**_DISPATCH, "github.ref": "refs/heads/release/new"},
        {**_DISPATCH, "github.ref": "refs/heads/feature/x"},
        {**_DISPATCH, "github.ref": "refs/tags/main"},
        {**_DISPATCH, "vars.SDLC_PIPELINE": ""},
        {**_DISPATCH, "vars.SDLC_PIPELINE": "disabled"},
        {"github.ref": "refs/heads/feature/x"},
    ]),
    "rc-publish-stage.yml": ([_STAGE, {**_STAGE, _RUN + "head_branch": "hotfix/new"}], [
        {**_STAGE, "vars.SDLC_PIPELINE": ""},
        {**_STAGE, _RUN + "conclusion": "failure"},
        {**_STAGE, _RUN + "event": "workflow_dispatch"},
        {**_STAGE, _RUN + "head_branch": "main"},
        {**_STAGE, _RUN + "head_repository.full_name": "fork/data-olympus"},
    ]),
}
ADMISSION["tag-release.yml"] = ([{"github.ref": "refs/heads/main"}], [
    {"github.ref": "refs/heads/feature/x"},
    {"github.ref": "refs/heads/release/new"},
    {"github.ref": "refs/tags/main"},
])
ADMISSION["hotfix-cut.yml"] = ADMISSION["recut-release-branch.yml"]
ADMISSION["promote-release.yml"] = ADMISSION["recut-release-branch.yml"]

_TOKEN = re.compile(r"\s*(?:('[^']*')|(&&|\|\||==|!=|\(|\)|,)|(format)\b|"
                    r"([A-Za-z_][\w-]*(?:\.[\w-]+)*))")


def evaluate(expression: str, context: dict) -> object:
    """Evaluate the GitHub expression subset used by concurrency groups."""
    body = expression.strip()
    match = re.fullmatch(r"\$\{\{(.*)\}\}", body, re.DOTALL)
    assert match, f"not an expression: {expression!r}"
    source, position, parts = match[1], 0, []
    while source[position:].strip():
        token = _TOKEN.match(source, position)
        assert token, f"unsupported expression syntax at {source[position:]!r}"
        string, operator, function, name = token.groups()
        if string is not None:
            parts.append(repr(string[1:-1]))
        elif operator is not None:
            parts.append({"&&": " and ", "||": " or "}.get(operator, f" {operator} "))
        elif function is not None:
            parts.append("_format")
        else:
            parts.append(f"_context({name!r})")
        position = token.end()

    def _format(template, *values):
        return re.sub(r"\{(\d+)\}", lambda m: str(values[int(m[1])]), template)

    def _context(name):
        return context.get(name)

    return eval("".join(parts), {"__builtins__": {}},  # noqa: S307 - tokens whitelisted
                {"_format": _format, "_context": _context})


def _workflow(name):
    path = WORKFLOWS / name
    if name in PENDING and not path.exists():
        pytest.skip(f"{name} is delivered by {PENDING[name]}; strict once it lands")
    assert path.exists(), f"missing privileged workflow: {name}"
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("name", [*DELIVERED, *PENDING])
def test_privileged_workflows_share_promotion_lock(name):
    workflow = _workflow(name)
    concurrency = workflow["concurrency"]
    assert set(concurrency) == {"group", "cancel-in-progress"}
    assert concurrency["cancel-in-progress"] is False
    group = concurrency["group"].strip()
    for job in workflow["jobs"].values():
        assert "concurrency" not in job, f"{name} must not override the lock per job"
    if name in UNCONDITIONAL:
        assert group == LOCK, f"{name} must take the bare shared lock, not a conditional"
        assert "inputs." not in group
        return
    if name not in DELIVERED and group == LOCK:
        return  # unconditional form, tolerated for workflows of other tasks
    assert LOCK in group
    fallback = re.search(r"format\('([\w.-]+-noop-)\{0\}', github\.run_id\)", group)
    assert fallback, f"{name} needs a run-scoped noop fallback group"
    admitted, refused = ADMISSION[name]
    for context in admitted:
        assert evaluate(group, {**context, "github.run_id": 101}) == LOCK, context
    for context in refused:
        first = evaluate(group, {**context, "github.run_id": 101})
        second = evaluate(group, {**context, "github.run_id": 102})
        assert first == f"{fallback[1]}101", context
        assert first != second, "a refused run's group must be run-scoped"



def test_evaluator_matches_task_four_conditional_form():
    """Task 4's multi-line group must evaluate here before its file lands."""
    group = """${{ (
      vars.SDLC_PIPELINE == 'enabled' &&
      github.event.workflow_run.conclusion == 'success' &&
      github.event.workflow_run.event == 'push' &&
      (github.event.workflow_run.head_branch == 'release/new' ||
       github.event.workflow_run.head_branch == 'hotfix/new') &&
      github.event.workflow_run.head_repository.full_name == github.repository
    ) && 'data-olympus-promotion' || format('rc-publish-noop-{0}', github.run_id) }}"""
    admitted, refused = ADMISSION["rc-publish-stage.yml"]
    for context in admitted:
        assert evaluate(group, {**context, "github.run_id": 7}) == LOCK
    for context in refused:
        assert evaluate(group, {**context, "github.run_id": 7}) == "rc-publish-noop-7"
