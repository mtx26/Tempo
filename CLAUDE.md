# Tempo — Claude Code operating contract

**ALWAYS reply to the user in French**, including summaries, questions, error reports and plans. Repository documentation and prompts may be in English. Do not change this rule.

## Mission
Build Tempo from scratch: an autonomous, self-hosted personal planner whose ONLY user-facing interface is Home Assistant via a custom integration distributable through HACS. The backend runs separately in Debian Docker Swarm; do not create a separate frontend or expose a public Tempo endpoint.

## First actions in every session
1. Read `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `docs/DECISIONS.md`, `PROGRESS.md`.
2. Inspect repository and available tools; do not assume a component exists.
3. Choose the smallest unblocked milestone; write a brief plan for material changes.
4. Implement with tests, lint and type checks; report actual results, not assumed success.
5. Update `PROGRESS.md`, tests, and relevant docs before ending.

## Non-negotiable behavior
- Fixed university iCal events remain fixed; PERSONAL STUDY hours are movable, including accepted blocks unless user has explicitly locked them. Flexible target dates are not hard deadlines.
- Training sessions are flexible; races/competitions are fixed.
- Source of actual location is the configured Home Assistant phone tracker. For future travel, infer origin from the event chain when possible, not stale GPS. No automatic father/mother-home switching.
- Travel is mandatory wherever needed, including walking, transfers, margins and return journeys when applicable.
- Preferred study blocks: 90 minutes, alternate subjects, obey availability; these are soft preferences, never stronger than feasibility.
- Changes to already accepted FIXED events or other protected commitments require approval; ordinary movable study sessions can be optimized automatically. On iCal change overnight, update source event and seek approval before moving protected commitments.
- Never fabricate schedules, real-time transit, locations or AI tool results. Report impossible plans clearly.
- AI interprets requests; deterministic code verifies rules, OR-Tools plans, MOTIS routes. Backend must continue essential functions when LLM is unavailable.
- Access only specifically authorized training emails, never entire mailbox by default.
- No production deployment, destructive commands, secret exposure, unapproved commits/pushes, or security-impacting changes without explicit approval. Never claim work continues after the session stops or usage limits are reached.
- Prefer incremental implementation, isolated test fixtures, security reviews, and truthful status. If lint/tests fail, state that work is incomplete.

## Project tooling
Use specialized `.claude/agents/` and `.claude/skills/` when useful. If Superpowers is installed, use its brainstorming, planning, test-driven development and systematic debugging workflows; do not assume plugin availability. Read installed documentation rather than inventing commands.

## Autonomous development policy

- Always communicate with the user in French.
- Continue through approved roadmap milestones without waiting for further instructions.
- Run tests, Ruff, mypy strict, and relevant security checks before completing a milestone.
- Update PROGRESS.md and the roadmap after each milestone.
- Automatically create a local Git commit for each validated milestone.
- Automatically start the next unfinished milestone.
- Do not stop merely to provide a progress summary.
- Local git init, branching, staging, and commits are authorized.
- Never push, publish, deploy, or modify production without explicit approval.
- Never commit secrets or sensitive personal data.
- When an external dependency is unavailable, document the limitation and continue independent work.
- Stop only for genuine blockers, required user decisions, safety concerns, or execution limits.
- Before any interruption, persist progress and the exact next steps.
