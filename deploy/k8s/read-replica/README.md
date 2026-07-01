# Read-only replica overlay (issue #44)

Horizontally scalable read serving for data-olympus. This overlay runs N
interchangeable, stateless read pods (a `Deployment`, not the writer
`StatefulSet`). Each replica clones the KB from the git remote into a per-pod
`emptyDir`, builds its own index, and refreshes both via the `git_pull_loop`.
`KB_READ_ONLY=true` (from `configmap.yaml`) makes the server register only the
read tools and read REST routes; the write pipeline (worktrees / push queue /
pending) is never initialised, so a replica is never a git writer. The single
`StatefulSet` remains the only writer/owner of the git remote.

At startup a read replica logs an unambiguous line:

```
starting in READ-ONLY mode; write pipeline disabled
```

If this line is absent from a replica's logs, the pod is misconfigured (wrong
image or the flag did not reach the container) and must not be trusted as a
read-only pod.

## Operator prerequisites (do these BEFORE `kubectl apply -k`)

Two prerequisites are operator/CI actions and are NOT performed by the manifests:

### 1. Build and publish a `KB_READ_ONLY`-capable image

`deployment.yaml` intentionally pins a placeholder tag
(`ghcr.io/knaisoma/data-olympus:read-only-preview`) that will fail to pull until
you replace it. Do NOT point it at the writer's published `v0.1.1` tag: that
image predates `KB_READ_ONLY`, ignores the flag, and would initialise the write
pipeline, turning the replica into a second git writer.

Build and publish an image from a revision that understands `KB_READ_ONLY`
(this feature branch or later), then pin it in `deployment.yaml` by tag or,
preferably, by immutable digest:

```
image: ghcr.io/knaisoma/data-olympus@sha256:<digest-of-your-build>
```

Verify a candidate image honours the flag before rolling it out: run it locally
with `KB_READ_ONLY=true` and confirm the startup log shows the READ-ONLY line
above and that `POST /api/v1/propose/memory` returns 404.

### 2. Provision a separate read-only git deploy key

The read Deployment references a Secret named `data-olympus-mcp-readonly` for
its git key and remote URL. It does NOT reuse the writer's `data-olympus-mcp`
Secret, whose deploy key has write (push) access. This keeps a write-capable key
off read pods by default.

Provision a dedicated read-only key using `secret.template.yaml` in this
directory:

1. Generate a key: `ssh-keygen -t ed25519 -f /tmp/ro-deploy-key -N "" -C "data-olympus-read"`
2. Register `/tmp/ro-deploy-key.pub` on the KB repo as a **read-only** deploy
   key (GitHub: add a deploy key WITHOUT "Allow write access"). If the host has
   no per-key permission control, use a read-only machine account.
3. `cp secret.template.yaml secret.yaml`, fill in the values, SOPS-encrypt to
   `secret.sops.yaml`, and apply it (commit only the `.sops.yaml`, never the
   plaintext).

## Apply

```
kubectl apply -k deploy/k8s              # namespace + writer secret + writer
# provision data-olympus-mcp-readonly (step 2 above)
kubectl apply -k deploy/k8s/read-replica # N read replicas
```

Scale reads with:

```
kubectl -n data-olympus scale deployment/data-olympus-mcp-read --replicas=N
```
