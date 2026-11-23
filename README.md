# Tempo

Autonomous personal planner for Home Assistant, developed with Claude Code. This repository is a **starter kit** (architecture, rules, agents and skills), **not a working application**.

## Quick start (Windows PowerShell)
```powershell
cd C:\Projects\Tempo
git init
claude
```
Then paste the bootstrap prompt from `docs/BOOTSTRAP_PROMPT.md`. See `docs/INSTALLATION.md` for full steps.

## Core constraints
Only Home Assistant is user-facing. Core: FastAPI, PostgreSQL, OR-Tools, worker, MOTIS; Belgian Mobility feeds subject to validation. No credentials in Git. Production actions require approval.
