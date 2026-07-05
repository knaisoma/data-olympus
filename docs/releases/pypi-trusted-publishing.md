# PyPI publishing (Trusted Publishing) — one-time operator setup

The release chain builds an sdist + wheel and uploads them to PyPI using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OpenID Connect).
**No API token is stored anywhere** (not in repo secrets, not in the workflow).
GitHub mints a short-lived OIDC token at release time and PyPI exchanges it for a
one-shot upload credential.

The publish workflow is **inert until you complete the pypi.org setup below.**
Until then, every release still tags, builds the container image, and publishes a
GitHub Release normally; only the PyPI upload step fails (and it is marked
`continue-on-error`, so it does not block the release). Once the pending
publisher exists, the next release uploads to PyPI with no further action.

## After setup: tighten the release chain

Once the publishers are configured and the first release has published to PyPI
for real, harden the chain so a broken upload can no longer pass silently. Do
NOT make these edits before setup: pre-setup the upload step fails by design, so
removing its tolerance would break every release.

Two edits, both post-first-successful-publish:

- In `.github/workflows/publish-pypi-reusable.yml`, remove the
  `continue-on-error: true` line from the **Publish to PyPI (Trusted
  Publishing)** step (the `pypa/gh-action-pypi-publish@release/v1` step). A real
  upload failure then fails the job instead of no-oping.
- In `.github/workflows/tag-release.yml`, add `publish-pypi` to the `release`
  job's `needs`, i.e. change `needs: [decide, build-image]` to
  `needs: [decide, build-image, publish-pypi]`. The GitHub Release is then cut
  only after the PyPI upload succeeds, so a release never advertises a version
  that failed to publish.

## What binds the publisher

Trusted Publishing matches four values exactly. Ours are:

- **PyPI project name:** `data-olympus`
- **Owner (GitHub org/user):** `knaisoma`
- **Repository:** `data-olympus`
- **Workflow filename:** `publish-pypi.yml`
- **Environment name:** `pypi`

The `environment: pypi` and `workflow filename` come from
`.github/workflows/publish-pypi-reusable.yml` (the reusable workflow that does the
upload) and `.github/workflows/publish-pypi.yml` (the caller PyPI knows about).
When PyPI validates the OIDC claim it checks the **top-level workflow** that
started the run. Both `publish-pypi.yml` (manual/tag fallback) and
`tag-release.yml` (the normal release path) call the reusable upload workflow, so
register a pending publisher for **each** top-level workflow filename you release
from (see step 2b).

## One-time steps on pypi.org

You do this once. There is no project yet, so create a **pending publisher**
(PyPI supports binding a publisher before the project's first upload).

### Step 1 — sign in

1. Go to <https://pypi.org> and log in (create an account first if needed).
2. Confirm 2FA is enabled on the account (PyPI requires it for uploads).

### Step 2 — add the pending publisher

1. Open <https://pypi.org/manage/account/publishing/>.
2. Scroll to **"Add a new pending publisher"** and select the **GitHub** tab.
3. Fill in **exactly**:
   - **PyPI Project Name:** `data-olympus`
   - **Owner:** `knaisoma`
   - **Repository name:** `data-olympus`
   - **Workflow name:** `publish-pypi.yml`
   - **Environment name:** `pypi`
4. Click **Add**.

### Step 2b — add the second publisher for the primary release path

The normal release runs from `tag-release.yml`, not `publish-pypi.yml`. Add a
second pending publisher identical to step 2 but with:

- **Workflow name:** `tag-release.yml`

(Everything else the same: owner `knaisoma`, repo `data-olympus`, environment
`pypi`, project `data-olympus`.) Without this, releases cut by the normal
main-merge flow would fail the OIDC check; the `publish-pypi.yml` publisher alone
only covers the manual-tag / dispatch fallback.

### Step 3 — create the GitHub `pypi` environment (recommended)

1. In the GitHub repo: **Settings → Environments → New environment**, name it
   `pypi`.
2. Optionally add required reviewers or a branch/tag protection rule so only
   release tags can deploy to it. (The workflow already scopes `id-token: write`
   to the `pypi` environment.)

## Verifying after a release

1. After the first release that runs with the publisher configured, check the
   **Publish to PyPI** job in the Actions run — the upload step should succeed.
2. Confirm the project page exists: <https://pypi.org/project/data-olympus/>.
3. Smoke-test the published artifact:

   ```bash
   uvx --from data-olympus data-olympus --help
   uv tool install data-olympus
   data-olympus setup --check
   ```

## PR dry-run (no setup needed)

Every PR that touches `pyproject.toml`, `src/`, `bin/`, or the publish workflows
runs a **dry run**: `uv build` + `twine check --strict` + a wheel smoke test, with
no upload. This catches packaging regressions before a release without needing
any PyPI credentials.
