# Incidents

## 2026-11-23 — `routing/config/` disappeared from the working tree

**Impact**: `routing/config/belgium.yml` and `brussels.yml` (tracked) were missing from disk;
noticed by `git status` (shown as deleted) at 20:14:58 UTC. Restored with
`git restore routing/config` at 20:16:10 UTC; content identical to the committed version
(`38a4aaa`). No data loss (files are versioned).

**Evidence collected (investigation 2026-11-23)**
- Directory mtime of `routing/` read at 20:15:04 UTC: 21:14 local (= 20:14 UTC). The deletion
  therefore happened between 20:14:00 and 20:14:59 UTC.
- Every tool command recorded in that minute was inspected in full text, from the main Claude
  Code session and the four sub-agents (transcripts in
  `~/.claude/projects/C--Users-mtx-2-Documents-Code-Tempo/<session>/subagents/*.jsonl`):
  - main session: Python patch of `backend/src/tempo/db/__main__.py`, ruff/pytest runs, creation
    of `deploy/backup/*.sh` (the words `mv`/`find -delete` are the script CONTENT written by a
    quoted heredoc, executed later only inside a container), then `git rm --cached graphify-out`
    (index only);
  - HA agent: writes inside `../tempo-homeassistant/custom_components/tempo` only;
  - security agent: read-only commands and a timing script in the session scratchpad.
  None references `routing/config` or deletes files in this repository.
- Other Claude Code sessions of this project were NOT active at that time (their transcripts end
  at 18:53 UTC or start at 21:58 UTC).
- graphify (`hook-guard` runs before every Read/Grep/Bash call) only lists these files in its
  index (`graphify-out/manifest.json`); no evidence that it deletes files.
- Docker Desktop had earlier bind-mounted `routing\config\*.yml` read-only into MOTIS import
  containers (10:02 UTC); no container used them at 20:14 UTC.

**Conclusion**: cause NOT determined. No recorded command of the development sessions explains
the deletion; an external process (editor, sync/antivirus tool, a hook) cannot be excluded or
confirmed with the available evidence.

**Mitigation / if it happens again**
- The files are versioned: `git status` shows the deletion; `git restore routing/config`.
- Enable Windows object-access auditing (or Sysinternals Process Monitor with a path filter on
  `routing\config`) to capture the deleting process.
- `deploy/stack.yml` does not read these files at runtime (MOTIS reads the config stored in its
  data volume), so production is not affected by such a deletion.
