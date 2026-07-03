"""`data-olympus setup`: guided first-run + update wizard.

Steps (idempotent; a re-run is the update path):

1. probe/choose the server endpoint (health check against /api/v1/health)
2. detect installed agents (Claude Code, Codex, Gemini, OpenCode)
3. write MCP registration config per agent, with timestamped backups
4. offer enforcement-hook install via the shipped bin/_kb_enforce.py machinery
5. doctor summary (endpoint, agents wired, hooks, version vs latest on PyPI)

`data-olympus setup --check` prints the doctor summary WITHOUT changing anything.
This is also the update-check path.

Design notes:

- All network and filesystem effects go through small, individually testable
  helpers. The interactive `run()` orchestrates; `check()` never mutates.
- Config writes always back up an existing file first (`<file>.do-bak-<ts>`)
  and MERGE into any existing MCP-server map rather than clobbering it.
- Agent registration uses the CURRENT surface per agent (verified against the
  live `claude mcp` / `codex mcp` CLIs), not stale docs. Claude Code and Codex
  prefer their CLI when present; Gemini and OpenCode merge documented config
  files. When a preferred CLI is absent we fall back to the documented file and
  print the exact manual command.
- No private/company endpoint is ever hard-coded: the default prompt is
  http://localhost:8080, matching the public example bundle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from data_olympus import __version__ as INSTALLED_VERSION

# Type aliases keep the long injection-seam signatures under the line limit.
CliRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
EnforceRunner = Callable[[list[str], dict[str, str]], "subprocess.CompletedProcess[str]"]
Opener = Callable[[str, float], object]
Fetcher = Callable[[str, float], bytes]

DEFAULT_ENDPOINT = "http://localhost:8080"
MCP_SERVER_NAME = "data-olympus"
_HEALTH_TIMEOUT = 5.0
_PYPI_TIMEOUT = 5.0
_BACKUP_SUFFIX = "do-bak"


# --------------------------------------------------------------------------- #
# Packaged bin/ resolution
# --------------------------------------------------------------------------- #
def bin_root() -> Path:
    """Directory holding the enforcement machinery (kb-enforce-hook, _kb_enforce.py).

    Prefers the copy shipped inside the installed wheel (data_olympus/_bin/), so a
    pip/uvx install with no repo checkout still works. Falls back to the repo-root
    bin/ for an editable dev install.
    """
    packaged = Path(__file__).resolve().parent / "_bin"
    if (packaged / "_kb_enforce.py").is_file():
        return packaged
    # Editable/dev layout: <repo>/src/data_olympus/setup_wizard.py -> <repo>/bin
    return Path(__file__).resolve().parents[2] / "bin"


def enforce_script() -> Path:
    return bin_root() / "_kb_enforce.py"


# --------------------------------------------------------------------------- #
# Endpoint probing
# --------------------------------------------------------------------------- #
@dataclass
class HealthResult:
    reachable: bool
    endpoint: str
    detail: str
    kb_commit: str | None = None


def probe_endpoint(
    endpoint: str, *, opener: Opener | None = None
) -> HealthResult:
    """GET <endpoint>/api/v1/health. Never raises; returns a HealthResult.

    `opener` is an injection seam for tests: called as opener(url, timeout) and
    expected to return an object with `.status` and `.read()` (a urlopen-like
    context manager is also accepted).
    """
    endpoint = endpoint.rstrip("/")
    url = f"{endpoint}/api/v1/health"
    _open = opener or _default_opener
    try:
        resp = _open(url, _HEALTH_TIMEOUT)
        # Support both a context manager (urlopen) and a bare response (tests):
        # enter the CM if present, else use the object directly.
        enter = getattr(resp, "__enter__", None)
        exit_ = getattr(resp, "__exit__", None)
        r = enter() if callable(enter) else resp
        try:
            status = getattr(r, "status", None)
            body = r.read()  # type: ignore[union-attr]
        finally:
            if callable(exit_):
                exit_(None, None, None)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return HealthResult(False, endpoint, f"cannot reach {url}: {exc}")
    if status != 200:
        return HealthResult(False, endpoint, f"{url} returned HTTP {status}")
    kb_commit = None
    try:
        payload = json.loads(body.decode() if isinstance(body, bytes) else body)
        if isinstance(payload, dict):
            kb_commit = payload.get("kb_commit") or payload.get("commit")
    except (ValueError, AttributeError):
        pass
    return HealthResult(True, endpoint, f"{url} reachable (HTTP 200)", kb_commit)


def _default_opener(url: str, timeout: float) -> object:  # pragma: no cover
    return urllib.request.urlopen(url, timeout=timeout)


# --------------------------------------------------------------------------- #
# Agent detection
# --------------------------------------------------------------------------- #
@dataclass
class Agent:
    key: str
    label: str
    # Detection: a config dir under HOME, and/or a binary on PATH.
    config_dir: str  # relative to HOME, e.g. ".claude"
    binaries: tuple[str, ...] = ()
    detected: bool = False
    detected_via: str = ""


def _home(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else dict(os.environ)
    override = env.get("KB_ENFORCE_HOME")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~"))


def _which(name: str, env: dict[str, str] | None = None) -> str | None:
    path = (env or os.environ).get("PATH")
    return shutil.which(name, path=path)


def detect_agents(
    *, env: dict[str, str] | None = None, home: Path | None = None
) -> list[Agent]:
    """Detect installed agents by config-dir presence or binary on PATH.

    Detection is deliberately permissive: EITHER a known config dir OR a known
    binary marks the agent as present, so the wizard offers to wire an agent the
    operator uses even before its first run created a config tree.
    """
    env = env if env is not None else dict(os.environ)
    home = home if home is not None else _home(env)
    specs = [
        Agent("claude-code", "Claude Code", ".claude", ("claude",)),
        Agent("codex", "Codex", ".codex", ("codex",)),
        Agent("gemini", "Gemini / Antigravity", ".gemini", ("gemini", "agy")),
        Agent("opencode", "OpenCode", ".config/opencode", ("opencode",)),
    ]
    out: list[Agent] = []
    for spec in specs:
        via = ""
        if (home / spec.config_dir).is_dir():
            via = f"config dir ~/{spec.config_dir}"
        else:
            for b in spec.binaries:
                if _which(b, env):
                    via = f"binary '{b}' on PATH"
                    break
        out.append(
            Agent(
                key=spec.key,
                label=spec.label,
                config_dir=spec.config_dir,
                binaries=spec.binaries,
                detected=bool(via),
                detected_via=via,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Config write helpers (backup + merge, idempotent)
# --------------------------------------------------------------------------- #
def backup_file(target: Path, *, now: float | None = None) -> Path | None:
    """Copy target to a timestamped sibling before it is edited. No-op if absent."""
    if not target.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    dest = target.with_suffix(target.suffix + f".{_BACKUP_SUFFIX}-{ts}")
    shutil.copy2(target, dest)
    return dest


def _load_json(target: Path) -> dict[str, object]:
    if target.exists() and target.read_text().strip():
        loaded = json.loads(target.read_text())
        return loaded if isinstance(loaded, dict) else {}
    return {}


def merge_mcp_json(
    existing: dict[str, object], *, name: str, url: str, extra: dict[str, object] | None = None
) -> tuple[dict[str, object], bool]:
    """Merge an HTTP MCP server entry into an mcpServers-style JSON map.

    Returns (new_doc, changed). Idempotent: re-running with the same url leaves
    the doc unchanged and returns changed=False.
    """
    doc: dict[str, object] = json.loads(json.dumps(existing))  # deep copy
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        doc["mcpServers"] = servers
    entry: dict[str, object] = {"url": url}
    if extra:
        entry.update(extra)
    if servers.get(name) == entry:
        return doc, False
    servers[name] = entry
    return doc, True


@dataclass
class WriteResult:
    agent: str
    action: str  # "cli", "file", "manual", "skipped", "unchanged"
    detail: str
    backup: Path | None = None
    changed: bool = False


def write_json_registration(
    target: Path,
    *,
    agent: str,
    name: str,
    url: str,
    extra: dict[str, object] | None = None,
    now: float | None = None,
) -> WriteResult:
    """Back up + merge an HTTP MCP entry into a JSON config file. Idempotent."""
    existing = _load_json(target)
    new_doc, changed = merge_mcp_json(existing, name=name, url=url, extra=extra)
    if not changed:
        return WriteResult(agent, "unchanged", f"{target} already registers '{name}'")
    backup = backup_file(target, now=now)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(new_doc, indent=2) + "\n")
    return WriteResult(
        agent, "file", f"wrote '{name}' -> {url} into {target}", backup=backup, changed=True
    )


# --------------------------------------------------------------------------- #
# Per-agent registration
# --------------------------------------------------------------------------- #
def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def register_claude(
    url: str,
    *,
    home: Path,
    env: dict[str, str],
    runner: CliRunner | None = None,
    now: float | None = None,
) -> WriteResult:
    """Register with Claude Code.

    Current surface (verified via `claude mcp add --help`):
        claude mcp add --transport http data-olympus <url>/mcp
    The old guidance (hand-editing ~/.claude/settings.json mcpServers) is stale;
    the CLI writes ~/.claude.json. Prefer the CLI when `claude` is on PATH, else
    print the exact manual command and skip a silent file write to avoid landing
    the entry in a file the CLI does not read.

    Even though the CLI owns the write, we back up its target file (~/.claude.json)
    first so the wizard's "every registration is backed up" guarantee holds across
    CLI-backed and file-backed agents alike.
    """
    mcp_url = url.rstrip("/") + "/mcp"
    if _which("claude", env):
        run = runner or _run_cli
        backup = backup_file(home / ".claude.json", now=now)
        argv = ["claude", "mcp", "add", "--transport", "http", MCP_SERVER_NAME, mcp_url]
        proc = run(argv)
        if proc.returncode == 0:
            return WriteResult(
                "claude-code", "cli", f"registered via `claude mcp add` -> {mcp_url}",
                backup=backup, changed=True,
            )
        # `claude mcp add` fails if the name already exists: treat as unchanged.
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "already exists" in combined.lower():
            return WriteResult(
                "claude-code", "unchanged",
                f"'{MCP_SERVER_NAME}' already registered with Claude Code",
            )
        return WriteResult(
            "claude-code", "manual",
            "claude CLI present but `mcp add` failed; run manually: "
            f"claude mcp add --transport http {MCP_SERVER_NAME} {mcp_url} "
            f"(error: {combined.strip()[:200]})",
        )
    return WriteResult(
        "claude-code", "manual",
        "claude CLI not on PATH. Run: "
        f"claude mcp add --transport http {MCP_SERVER_NAME} {mcp_url}",
    )


def register_codex(
    url: str,
    *,
    home: Path,
    env: dict[str, str],
    runner: CliRunner | None = None,
    now: float | None = None,
) -> WriteResult:
    """Register with Codex.

    Current surface (verified via `codex mcp add --help`):
        codex mcp add data-olympus --url <url>/mcp
    Prefer the CLI when `codex` is on PATH; else print the exact command. Codex
    stores servers under [mcp_servers.<name>] in ~/.codex/config.toml (TOML). We
    do not hand-write that file, but we back it up before the CLI mutates it so
    the wizard's backup guarantee holds for CLI-backed agents too.
    """
    mcp_url = url.rstrip("/") + "/mcp"
    if _which("codex", env):
        run = runner or _run_cli
        backup = backup_file(home / ".codex" / "config.toml", now=now)
        argv = ["codex", "mcp", "add", MCP_SERVER_NAME, "--url", mcp_url]
        proc = run(argv)
        if proc.returncode == 0:
            return WriteResult(
                "codex", "cli", f"registered via `codex mcp add` -> {mcp_url}",
                backup=backup, changed=True,
            )
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "already" in combined.lower() and "exist" in combined.lower():
            return WriteResult(
                "codex", "unchanged", f"'{MCP_SERVER_NAME}' already registered with Codex"
            )
        return WriteResult(
            "codex", "manual",
            "codex CLI present but `mcp add` failed; run manually: "
            f"codex mcp add {MCP_SERVER_NAME} --url {mcp_url} "
            f"(error: {combined.strip()[:200]})",
        )
    return WriteResult(
        "codex", "manual",
        f"codex CLI not on PATH. Run: codex mcp add {MCP_SERVER_NAME} --url {mcp_url}",
    )


def register_gemini(url: str, *, home: Path, now: float | None = None) -> WriteResult:
    """Register with Gemini / Antigravity by merging ~/.gemini/settings.json.

    Gemini's MCP servers live under mcpServers with an explicit "type": "http"
    (not "transport"). No stable `gemini mcp add` CLI surface is assumed, so we
    write the documented file with a backup.
    """
    mcp_url = url.rstrip("/") + "/mcp"
    target = home / ".gemini" / "settings.json"
    return write_json_registration(
        target, agent="gemini", name=MCP_SERVER_NAME, url=mcp_url,
        extra={"type": "http"}, now=now,
    )


def register_opencode(url: str, *, home: Path, now: float | None = None) -> WriteResult:
    """Register with OpenCode by merging ~/.config/opencode/opencode.json.

    OpenCode has no native remote-HTTP MCP transport; it wraps the endpoint as a
    local command through mcp-remote. The server map lives under the top-level
    "mcp" key (not "mcpServers").
    """
    mcp_url = url.rstrip("/") + "/mcp"
    target = home / ".config" / "opencode" / "opencode.json"
    entry = {
        "type": "local",
        "command": ["npx", "-y", "mcp-remote", mcp_url, "--allow-http"],
        "enabled": True,
    }
    existing = _load_json(target)
    doc: dict[str, object] = json.loads(json.dumps(existing))
    servers = doc.get("mcp")
    if not isinstance(servers, dict):
        servers = {}
        doc["mcp"] = servers
    if servers.get(MCP_SERVER_NAME) == entry:
        return WriteResult(
            "opencode", "unchanged", f"{target} already registers '{MCP_SERVER_NAME}'"
        )
    servers[MCP_SERVER_NAME] = entry
    backup = backup_file(target, now=now)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2) + "\n")
    return WriteResult(
        "opencode", "file", f"wrote '{MCP_SERVER_NAME}' -> {mcp_url} into {target}",
        backup=backup, changed=True,
    )


def register_agent(
    key: str,
    url: str,
    *,
    home: Path,
    env: dict[str, str],
    runner: CliRunner | None = None,
    now: float | None = None,
) -> WriteResult:
    if key == "claude-code":
        return register_claude(url, home=home, env=env, runner=runner, now=now)
    if key == "codex":
        return register_codex(url, home=home, env=env, runner=runner, now=now)
    if key == "gemini":
        return register_gemini(url, home=home, now=now)
    if key == "opencode":
        return register_opencode(url, home=home, now=now)
    return WriteResult(key, "skipped", f"no registration handler for '{key}'")


# --------------------------------------------------------------------------- #
# Enforcement hooks (delegates to the shipped _kb_enforce.py)
# --------------------------------------------------------------------------- #
def install_enforcement(
    agent_key: str,
    *,
    env: dict[str, str],
    runner: EnforceRunner | None = None,
) -> WriteResult:
    """Invoke the shipped provider registry: `_kb_enforce.py install --agent <key>`.

    The provider backs up the target settings file and writes MARKER-tagged hook
    entries; it is itself idempotent, so re-running is safe.
    """
    script = enforce_script()
    if not script.is_file():
        return WriteResult(
            agent_key, "skipped",
            f"enforcement machinery not found at {script}; skipping hook install",
        )
    argv = [sys.executable, str(script), "install", "--agent", agent_key]
    run = runner or _run_enforce
    proc = run(argv, env)
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode == 0:
        return WriteResult(agent_key, "file", detail or "enforcement installed", changed=True)
    return WriteResult(
        agent_key, "manual",
        f"enforcement install failed (rc={proc.returncode}): {detail[:200]}",
    )


def _run_enforce(  # pragma: no cover
    argv: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)


def enforcement_status(
    *,
    env: dict[str, str],
    runner: EnforceRunner | None = None,
) -> str:
    """Return the multi-agent `_kb_enforce.py status` output (read-only)."""
    script = enforce_script()
    if not script.is_file():
        return f"enforcement machinery not found at {script}"
    argv = [sys.executable, str(script), "status"]
    run = runner or _run_enforce
    proc = run(argv, env)
    return (proc.stdout or "").strip() or (proc.stderr or "").strip() or "(no status output)"


# --------------------------------------------------------------------------- #
# Version check (offline-tolerant)
# --------------------------------------------------------------------------- #
@dataclass
class VersionInfo:
    installed: str
    latest: str | None
    source: str  # "pypi", "github", "offline"
    detail: str = ""


def latest_version(
    *, fetcher: Fetcher | None = None
) -> VersionInfo:
    """Look up the latest published version, tolerant of being offline.

    Tries PyPI's JSON API first, then the GitHub releases API. Any failure
    degrades to source="offline" with latest=None; the wizard never errors on a
    missing network.
    """
    fetch = fetcher or _default_fetch
    # 1. PyPI
    try:
        raw = fetch("https://pypi.org/pypi/data-olympus/json", _PYPI_TIMEOUT)
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        latest = data.get("info", {}).get("version")
        if latest:
            return VersionInfo(INSTALLED_VERSION, latest, "pypi")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    # 2. GitHub releases (works even before the first PyPI publish)
    try:
        raw = fetch(
            "https://api.github.com/repos/knaisoma/data-olympus/releases/latest",
            _PYPI_TIMEOUT,
        )
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        tag = data.get("tag_name")
        if tag:
            return VersionInfo(INSTALLED_VERSION, tag.lstrip("v"), "github")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return VersionInfo(
        INSTALLED_VERSION, None, "offline",
        "could not reach PyPI or GitHub; skipping update check",
    )


def _default_fetch(url: str, timeout: float) -> bytes:  # pragma: no cover
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data: bytes = r.read()
    return data


def _version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split("+")[0].split("-")[0].split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def compare_versions(info: VersionInfo) -> str:
    """Human-readable one-liner about installed vs latest."""
    if info.latest is None:
        return f"version {info.installed} installed ({info.detail})"
    try:
        newer = _version_tuple(info.latest) > _version_tuple(info.installed)
    except ValueError:
        newer = info.installed != info.latest
    if newer:
        return (
            f"version {info.installed} installed; {info.latest} available on "
            f"{info.source} (run `uv tool upgrade data-olympus`)"
        )
    return f"version {info.installed} installed (latest on {info.source})"


# --------------------------------------------------------------------------- #
# Doctor summary (read-only)
# --------------------------------------------------------------------------- #
@dataclass
class Doctor:
    endpoint: HealthResult
    agents: list[Agent]
    enforcement: str
    version: str
    lines: list[str] = field(default_factory=list)


def build_doctor(
    endpoint: str,
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    opener: Opener | None = None,
    version_fetcher: Fetcher | None = None,
    enforce_runner: EnforceRunner | None = None,
    check_version: bool = True,
) -> Doctor:
    """Assemble a read-only doctor report. Performs NO writes."""
    env = env if env is not None else dict(os.environ)
    home = home if home is not None else _home(env)
    health = probe_endpoint(endpoint, opener=opener)
    agents = detect_agents(env=env, home=home)
    enforcement = enforcement_status(env=env, runner=enforce_runner)
    if check_version:
        ver_line = compare_versions(latest_version(fetcher=version_fetcher))
    else:
        ver_line = f"version {INSTALLED_VERSION} installed (version check skipped)"

    lines: list[str] = []
    ep_mark = "OK" if health.reachable else "!!"
    lines.append(f"[{ep_mark}] endpoint: {health.detail}")
    detected = [a for a in agents if a.detected]
    if detected:
        for a in detected:
            lines.append(f"[OK] agent: {a.label} detected ({a.detected_via})")
    else:
        lines.append("[--] agent: none detected")
    for a in agents:
        if not a.detected:
            lines.append(f"[--] agent: {a.label} not detected")
    lines.append("[..] enforcement hooks:")
    for eline in enforcement.splitlines():
        lines.append(f"       {eline}")
    lines.append(f"[..] {ver_line}")
    return Doctor(health, agents, enforcement, ver_line, lines)


# --------------------------------------------------------------------------- #
# Interactive orchestration
# --------------------------------------------------------------------------- #
def _prompt(text: str, default: str, *, stream_in: TextIO, stream_out: TextIO) -> str:
    stream_out.write(f"{text} [{default}]: ")
    stream_out.flush()
    line = stream_in.readline()
    if not line:
        return default
    line = line.strip()
    return line or default


def _confirm(text: str, *, default: bool, stream_in: TextIO, stream_out: TextIO) -> bool:
    suffix = "Y/n" if default else "y/N"
    stream_out.write(f"{text} [{suffix}]: ")
    stream_out.flush()
    line = stream_in.readline()
    if not line:
        return default
    ans = line.strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def run(
    *,
    argv_endpoint: str | None = None,
    check_only: bool = False,
    assume_yes: bool = False,
    no_version_check: bool = False,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    stream_in: TextIO | None = None,
    stream_out: TextIO | None = None,
    opener: Opener | None = None,
) -> int:
    """Entry point behind `data-olympus setup`.

    check_only=True is fully read-only (the `--check` / update-check path).
    """
    env = env if env is not None else dict(os.environ)
    home = home if home is not None else _home(env)
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    interactive = stream_in.isatty() if hasattr(stream_in, "isatty") else False

    def out(msg: str = "") -> None:
        stream_out.write(msg + "\n")

    if check_only:
        doctor = build_doctor(
            argv_endpoint or env.get("KB_ENDPOINT", DEFAULT_ENDPOINT),
            env=env, home=home, opener=opener, check_version=not no_version_check,
        )
        out("data-olympus setup --check")
        out("")
        for line in doctor.lines:
            out(line)
        return 0

    out("data-olympus setup")
    out("=" * 40)

    # 1. Endpoint
    default_ep = argv_endpoint or env.get("KB_ENDPOINT", DEFAULT_ENDPOINT)
    if interactive and not assume_yes:
        endpoint = _prompt(
            "Server endpoint", default_ep, stream_in=stream_in, stream_out=stream_out
        )
    else:
        endpoint = default_ep
    health = probe_endpoint(endpoint, opener=opener)
    mark = "OK" if health.reachable else "WARNING"
    out(f"[{mark}] {health.detail}")
    if not health.reachable:
        out(
            "  The endpoint is not reachable now. Registration will still be written; "
            "start the server and re-run `data-olympus setup --check` to verify."
        )

    # 2. Detect agents
    out("")
    out("Detecting agents...")
    agents = detect_agents(env=env, home=home)
    detected = [a for a in agents if a.detected]
    if not detected:
        out("  No agents detected. Nothing to wire.")
    for a in detected:
        out(f"  - {a.label} ({a.detected_via})")

    # 3 + 4. Register + enforcement, per detected agent
    for a in detected:
        do_reg = assume_yes or not interactive or _confirm(
            f"Register data-olympus MCP with {a.label}?",
            default=True, stream_in=stream_in, stream_out=stream_out,
        )
        if do_reg:
            res = register_agent(a.key, endpoint, home=home, env=env)
            out(f"  [{res.action}] {a.label}: {res.detail}")
            if res.backup:
                out(f"        backup: {res.backup}")
        do_hook = assume_yes or not interactive or _confirm(
            f"Install enforcement hooks for {a.label}?",
            default=False, stream_in=stream_in, stream_out=stream_out,
        )
        if do_hook:
            hres = install_enforcement(a.key, env=env)
            out(f"  [{hres.action}] {a.label} hooks: {hres.detail}")

    # 5. Doctor
    out("")
    out("Doctor summary")
    out("-" * 40)
    doctor = build_doctor(
        endpoint, env=env, home=home, opener=opener, check_version=not no_version_check,
    )
    for line in doctor.lines:
        out(line)
    return 0
