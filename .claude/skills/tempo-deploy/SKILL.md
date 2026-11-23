---
name: tempo-deploy
description: Prepare reversible Swarm deployment, health checks, backups, least privilege, secret management and rollback. Do not deploy without explicit approval.
---
# tempo-deploy

**Always respond to the user in French.**

## Workflow
1. Read `CLAUDE.md`, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md` and `PROGRESS.md`.
2. Prepare reversible Swarm deployment, health checks, backups, least privilege, secret management and rollback. Do not deploy without explicit approval.
3. Identify hard constraints, security boundaries and testable acceptance criteria.
4. Perform safe local changes only; request approval for production/destructive operations.
5. Run and report relevant tests, lint, type checks and integration checks (or explicitly say why unavailable).
6. Update `PROGRESS.md` and relevant docs, listing completed work, limitations and next step.
