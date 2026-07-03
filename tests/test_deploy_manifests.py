"""Sanity checks on the deploy manifests (WP3a container hardening + probe split).

Prefers a real ``kubectl kustomize`` build when the binary is available so the
kustomization actually renders; otherwise falls back to parsing the individual
YAML documents. Both paths assert the security posture is what WP3a set out to
achieve: rootless containers, no gosu/added caps, Ingress excluded from the
default apply, and the readiness probe pointed at /readyz.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

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
