---
name: debug
description: Reproduce the failure, isolate root cause, create regression test, apply minimal fix, run relevant tests/lint and report evidence.
---
# debug

**Always respond to the user in French.**

## Workflow
1. Read `CLAUDE.md`, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md` and `PROGRESS.md`.
2. Reproduce the failure, isolate root cause, create regression test, apply minimal fix, run relevant tests/lint and report evidence.
3. Identify hard constraints, security boundaries and testable acceptance criteria.
4. Perform safe local changes only; request approval for production/destructive operations.
5. Run and report relevant tests, lint, type checks and integration checks (or explicitly say why unavailable).
6. Update `PROGRESS.md` and relevant docs, listing completed work, limitations and next step.
