# Project agent rules

This preamble sits above the first real heading. It describes the overall
philosophy of the project and how agents should behave when working here. It is
several sentences long so that it clears the minimum body threshold and becomes
its own draft concept rather than being dropped.

## Writing style

Do not use em-dashes. Use commas, parentheses, or two sentences instead.
Follow the house style guide for length and tone. Keep documentation concise
and prefer active voice throughout every document you produce.

## Git workflow

Never bypass pre-commit hooks. Always verify the branch is up to date with the
remote before pushing. Use conventional commit messages for every commit so the
changelog can be generated automatically from history.

## Security defaults

Never write credentials, secrets, or passwords to any file. All secrets must be
prompted interactively or retrieved from the secrets manager. Never paste secret
values into chat, plan files, or commit messages.

## Deployment notes

- Remember to configure the database connection pool.
- The deployment script should retry on transient failures.
- Kubernetes readiness probes need a longer initial delay for the API service.
- Docker images must be rebuilt when the base image rotates its SSH host key.

## TODO

Fix later.
