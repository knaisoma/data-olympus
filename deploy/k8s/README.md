# data-olympus Kubernetes manifests

Base stack for the single-writer MCP server: namespace, configmap, Service,
NetworkPolicy, and the writer StatefulSet. Apply with kustomize:

```bash
# 1. Apply the SOPS-encrypted Secret (kustomize can't decrypt SOPS in-flow):
sops exec-file secret.sops.yaml 'kubectl apply -f {}'
# 2. Apply the base stack:
kubectl apply -k .
```

## Two resources are intentionally excluded from the default apply

Both are opt-in because applying them by default would be unsafe:

- **`secret.sops.yaml`** — SOPS-encrypted, applied separately (step 1 above) so
  the plaintext key never lands on disk.
- **`ingress.yaml`** — the Ingress publishes **all** routes, including the write
  and enforcement REST routes (`/api/v1/propose/*`, `/api/v1/resolve/*`,
  `/api/v1/onboarding/bootstrap`, `/api/v1/consult`, `/api/v1/gate/check`). With
  `KB_AUTH_TOKEN` unset that is an **unauthenticated write surface reachable from
  wherever the ingress controller is** (the LAN, by default). The default
  `kubectl apply -k .` therefore exposes only the in-cluster `Service`.

### Enabling the Ingress (after you have set auth)

1. Set `KB_AUTH_TOKEN` (and optionally `KB_AUTH_PRINCIPALS`) in the Secret so the
   write + enforcement routes require a bearer token. Read routes stay open by
   design; if reads must be confidential too, also restrict to a trusted network
   or front the Ingress with an authenticating reverse proxy.
2. Edit `ingress.yaml`: replace the `host`, and uncomment the TLS block +
   cert-manager annotation so write routes are never served over plaintext.
3. Uncomment `- ingress.yaml` in `kustomization.yaml`.
4. Re-apply: `kubectl apply -k .`.

See the repository `SECURITY.md` for the full threat model.

## Security posture

- **Rootless containers.** Both the `prepare-git` initContainer and the main
  container run as the non-root uid `65534` with `runAsNonRoot: true`, all
  Linux capabilities dropped, `allowPrivilegeEscalation: false`, and a read-only
  root filesystem. `fsGroup: 65534` gives the non-root process write access to the
  PVCs and read access to the deploy-key Secret. The manifest passes the
  **restricted** Pod Security Standard.
- **Deploy-key flow.** The old root+`gosu` entrypoint is gone. The initContainer
  stages the SSH deploy key to `/state/git-key` (mode `0400`) on the shared state
  volume and performs the first-boot `/kb-main` clone, all as non-root.
- **Probes.** Readiness targets `/readyz` (process up + index loaded, independent
  of data staleness) so a stale-KB pod is not ejected from the Service. Liveness
  is a `tcpSocket` check (an HTTP `/livez` route also exists). `/api/v1/health`
  keeps its 503-on-degraded contract for the CLI `--no-stale` flag; alert on it,
  do not probe it. See `../../docs/operations.md` for the full alerting model.

## Read replicas

`read-replica/` holds an overlay of N read-only replicas (a Deployment, not the
writer StatefulSet). See `../../docs/serving.md`.
