"""Sanity checks on the deploy manifests (WP3a container hardening + probe split).

Prefers a real ``kubectl kustomize`` build when the binary is available so the
kustomization actually renders; otherwise falls back to parsing the individual
YAML documents. Both paths assert the security posture is what WP3a set out to
achieve: rootless containers, no gosu/added caps, Ingress excluded from the
default apply, and the readiness probe pointed at /readyz.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

K8S_DIR = Path(__file__).resolve().parents[1] / "deploy" / "k8s"
DOCKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "docker"


def _load_all(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]


def _kustomize_build() -> list[dict] | None:
    """Render `kubectl kustomize deploy/k8s` if kubectl is available, else None."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return None
    try:
        out = subprocess.run(
            [kubectl, "kustomize", str(K8S_DIR)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]


def _rendered_docs() -> list[dict]:
    docs = _kustomize_build()
    if docs is not None:
        return docs
    # Fallback: parse the resources listed in kustomization.yaml directly.
    kz = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text())
    docs = []
    for res in kz.get("resources", []):
        docs.extend(_load_all(K8S_DIR / res))
    return docs


def _statefulset() -> dict:
    for d in _rendered_docs():
        if d.get("kind") == "StatefulSet":
            return d
    raise AssertionError("StatefulSet not found in rendered manifests")


def test_kustomization_yaml_parses() -> None:
    kz = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text())
    assert kz["kind"] == "Kustomization"
    assert "statefulset.yaml" in kz["resources"]


def test_ingress_excluded_from_default_kustomization() -> None:
    """The default apply must NOT include the Ingress (unauthenticated write
    surface). It is opt-in, mirroring how the secret is applied separately."""
    kz = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text())
    assert "ingress.yaml" not in kz.get("resources", [])
    kinds = {d.get("kind") for d in _rendered_docs()}
    assert "Ingress" not in kinds
    # But the file still exists for opt-in enablement.
    assert (K8S_DIR / "ingress.yaml").exists()


def test_pod_runs_as_nonroot() -> None:
    sts = _statefulset()
    pod_sc = sts["spec"]["template"]["spec"]["securityContext"]
    assert pod_sc["runAsNonRoot"] is True
    assert pod_sc["runAsUser"] == 65534
    assert pod_sc["fsGroup"] == 65534


def test_main_container_drops_all_caps_no_adds() -> None:
    sts = _statefulset()
    container = sts["spec"]["template"]["spec"]["containers"][0]
    sc = container["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    caps = sc["capabilities"]
    assert caps["drop"] == ["ALL"]
    # The old manifest added CHOWN/DAC_OVERRIDE/FOWNER/SETGID/SETUID for the
    # root+gosu phase. That phase is gone; no caps may be added back.
    assert "add" not in caps or caps["add"] == []


def test_initcontainer_stages_key_and_clones() -> None:
    sts = _statefulset()
    inits = sts["spec"]["template"]["spec"].get("initContainers", [])
    assert inits, "expected a prepare-git initContainer"
    init = inits[0]
    assert init["name"] == "prepare-git"
    assert init["securityContext"]["runAsNonRoot"] is True
    assert init["securityContext"]["capabilities"]["drop"] == ["ALL"]
    script = " ".join(init.get("args", []))
    assert "/state/git-key" in script  # stages the key
    assert "git clone" in script       # first-boot clone


def test_readiness_probe_points_at_readyz() -> None:
    sts = _statefulset()
    container = sts["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"


def test_image_tag_supports_readyz_and_rootless_flow() -> None:
    """The manifest now depends on /readyz and the rootless /state/git-key flow,
    both introduced in v0.3.0. Applying it against an older image (e.g. the old
    placeholder v0.1.1) would leave the pod NotReady and break the deploy key, so
    both containers must reference a tag >= v0.3.0 and share the same tag."""
    sts = _statefulset()
    spec = sts["spec"]["template"]["spec"]
    main_img = spec["containers"][0]["image"]
    init_img = spec["initContainers"][0]["image"]
    # Both point at the same image (init prepares state the main container reads).
    assert main_img == init_img, (main_img, init_img)
    # Must not be a pre-/readyz tag. Guard against the specific stale placeholder
    # and, generically, any v0.1.x / v0.2.x tag.
    for img in (main_img, init_img):
        tag = img.rsplit(":", 1)[-1]
        assert tag != "v0.1.1", f"stale placeholder image tag: {img}"
        assert not tag.startswith(("v0.1.", "v0.2.")), (
            f"image tag {tag} predates /readyz + rootless flow (need >= v0.3.0)"
        )


def test_dockerfile_is_digest_pinned_and_rootless() -> None:
    text = (DOCKER_DIR / "Dockerfile").read_text()
    assert "python:3.13-slim@sha256:" in text  # digest-pinned base
    assert "USER 65534:65534" in text          # runs non-root
    assert "KB_SSH_KEYSCAN_HOST" in text       # keyscan host configurable
    # gosu must not be installed or exec'd (a comment may mention it by name to
    # explain its removal, so only inspect non-comment directive lines).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert "gosu" not in stripped, f"gosu referenced in directive: {line}"


def test_entrypoint_has_no_gosu_or_chown() -> None:
    text = (DOCKER_DIR / "entrypoint.sh").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert "gosu" not in stripped, f"gosu in entrypoint: {line}"
        # No chown: we run as the target uid already, nothing to re-own.
        assert "chown" not in stripped, f"chown in entrypoint: {line}"


def test_compose_binds_loopback() -> None:
    text = (DOCKER_DIR / "compose.yaml").read_text()
    assert "127.0.0.1:8080:8080" in text


# --- First-boot clone: run the real block, do not grep it --------------------
# The bootstrap block decides which of two variables names the remote and logs a
# line before cloning. Both are behaviours, so they are exercised rather than
# matched as strings: the block is lifted out of entrypoint.sh unmodified and run
# with a stub `git` that records its argv.

BOOTSTRAP_MARKER = "# --- Bootstrap /kb-main on first boot"

# The container mount point cannot exist on the test host, so it is redirected
# into the tmp dir. Nothing else about the block is rewritten.
KB_MAIN_MOUNT = "/kb-main"


def _bootstrap_block() -> str:
    """The first-boot clone block, taken verbatim from entrypoint.sh.

    The surrounding script writes to fixed absolute paths (/tmp/known_hosts) and
    ends in ``exec "$@"``, so running it whole on a test host is not an option.
    """
    text = (DOCKER_DIR / "entrypoint.sh").read_text()
    start = text.index(BOOTSTRAP_MARKER)
    end = text.index('exec "$@"', start)
    return text[start:end]


def _run_bootstrap(tmp_path: Path, env: dict[str, str]) -> tuple[str, list[str]]:
    """Run the block with a stub git; return its output and git's argv.

    The stub prints nothing, so everything captured is the block's own output.
    """
    kb_main = tmp_path / "kb-main"
    kb_main.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "git-argv"
    git_stub = bin_dir / "git"
    git_stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{argv_file}"\n')
    git_stub.chmod(0o755)

    script = tmp_path / "bootstrap.sh"
    script.write_text("set -e\n" + _bootstrap_block().replace(KB_MAIN_MOUNT, str(kb_main)))

    proc = subprocess.run(
        ["sh", str(script)],
        capture_output=True, text=True, timeout=30,
        env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", **env},
    )
    assert proc.returncode == 0, proc.stderr
    argv = argv_file.read_text().splitlines() if argv_file.exists() else []
    return proc.stdout + proc.stderr, argv


requires_sh = pytest.mark.skipif(os.name != "posix", reason="POSIX shell required")


@requires_sh
def test_bootstrap_clones_when_only_kb_remote_url_is_set(tmp_path: Path) -> None:
    """The compose path carries only KB_REMOTE_URL -- it is the documented one,
    and the only remote variable compose.yaml sets. Without the fallback the
    clone never ran there and the server came up read-write on an unbootstrapped
    /kb-main, whose first symptom was the pull loop failing `rev-parse HEAD`."""
    _, argv = _run_bootstrap(tmp_path, {"KB_REMOTE_URL": "ssh://git@example.test/kb.git"})
    assert "clone" in argv
    assert "ssh://git@example.test/kb.git" in argv


@requires_sh
def test_bootstrap_prefers_kb_git_remote_url(tmp_path: Path) -> None:
    """k8s sets both from one secret key and its initContainer has already
    cloned, so the clone source must not change there."""
    _, argv = _run_bootstrap(tmp_path, {
        "KB_GIT_REMOTE_URL": "ssh://git@example.test/clone-source.git",
        "KB_REMOTE_URL": "ssh://git@example.test/push-target.git",
    })
    assert "ssh://git@example.test/clone-source.git" in argv
    assert "ssh://git@example.test/push-target.git" not in argv


@requires_sh
def test_bootstrap_is_a_no_op_with_no_remote_configured(tmp_path: Path) -> None:
    """compose.yaml ships KB_REMOTE_URL empty; the server then runs read-only and
    there is nothing to clone."""
    out, argv = _run_bootstrap(tmp_path, {"KB_REMOTE_URL": ""})
    assert argv == []
    assert out.strip() == ""


@requires_sh
def test_bootstrap_log_does_not_leak_a_credential(tmp_path: Path) -> None:
    """KB_REMOTE_URL is documented as an "SSH or HTTPS" push target and the
    compose path mounts no SSH key, so a writable remote there is most likely an
    HTTPS URL carrying a token. The pre-clone log line must not put it in the
    container log, where no application-level redaction can reach it."""
    url = "https://kb-bot:s3cr3t-token@example.test/kb.git"
    out, argv = _run_bootstrap(tmp_path, {"KB_REMOTE_URL": url})
    assert url in argv, "sanity: the clone still uses the configured remote"
    assert "s3cr3t-token" not in out
    assert "example.test" not in out


def test_compose_documents_that_kb_remote_url_also_bootstraps() -> None:
    """A reader of compose.yaml should not have to discover the fallback by
    watching the pull loop fail."""
    text = (DOCKER_DIR / "compose.yaml").read_text()
    assert "KB_GIT_REMOTE_URL" in text
