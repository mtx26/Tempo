---
name: implement-feature
description: Implement one approved Tempo feature end-to-end. Inspect context, define acceptance criteria, write tests first when practical, implement, run checks, update ARCHITECTURE.md / README.md.
---
# implement-feature

**Always respond to the user in French.**

## Workflow
1. Read `CLAUDE.md` and the relevant sections of `ARCHITECTURE.md` (and `README.md` for operations).
2. Implement one approved Tempo feature end-to-end. Inspect context, define acceptance criteria, write tests first when practical, implement, run checks, update ARCHITECTURE.md / README.md.
3. Identify hard constraints, security boundaries and testable acceptance criteria.
4. Perform safe local changes only; request approval for production/destructive operations.
5. Run and report relevant tests, lint, type checks and integration checks (or explicitly say why unavailable).
6. Update `ARCHITECTURE.md` (status and gaps) and `README.md` when behaviour or operations change; report limitations truthfully. No progress/handoff/roadmap files.
