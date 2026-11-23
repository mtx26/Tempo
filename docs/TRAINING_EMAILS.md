# Training-plan e-mails (milestone M5) — specification

Status (2026-11-23): **documented, real format NOT yet implemented.** The authorised sender's
exact e-mail address is **not yet known/configured** (user action required, see "Open items").

## Source

- The user's running coach, **Jean-Charles Engster**, sends a weekly training plan by e-mail.
- Typical message:
  - sender display name: `Jean-Charles Engster` (the display name is NOT an identity: only the
    exact, user-authorised e-mail address counts — still to be configured);
  - subject: `plan`;
  - body: `Confirme la réception` (no training content in the body);
  - attachment: a Microsoft Word `.docx`, e.g. `Plan Matis (1).docx` — **the plan is inside the
    attachment**.

## DOCX structure (observed)

- One weekly training table.
- A date range such as `Semaine du 23 au 29/11/2026` (gives the year and the Monday-Sunday dates).
- Columns: Monday → Sunday.
- Rows: training objective, session theme, detailed workout instructions.
- Some days have no training (empty cells).
- Instructions may contain durations, distances, repetitions, recovery intervals, intensity and
  terrain requirements.

Example (from the provided plan, week of 23–29/11/2026):

| Day | Content |
|---|---|
| Wednesday | VMA test, with `RAC : 10'` (warm-up/return to calm notation) |
| Friday | Fundamental endurance `1h15'`, with `3x80m` and `r 30"` at the end of the run |
| Sunday | Long run `1h25'`, hilly terrain, uphill accelerations |

Notation to support: `1h15'` = 1 h 15 min, `10'` = 10 min, `30"` = 30 s, `3x80m` = 3 repetitions
of 80 m, `r 30"` = 30 s recovery.

## Rules (binding for the implementation)

1. **Access**: only e-mails from the explicitly authorised sender address are read (server-side
   `FROM` filter + exact re-check + sender authentication DMARC/DKIM; read-only mailbox). Never the
   whole mailbox. Attachments of other senders are never downloaded.
2. **Extraction**: the plan is read from the `.docx` attachment (table cells by weekday column),
   not from the body. The date range gives the concrete dates.
3. **Flexibility**: the coach's weekday is a **suggestion, not a commitment**. Tempo may move a
   session to another day/time to optimise the whole calendar (trainings are flexible; races and
   competitions stay fixed).
4. **Content preserved**: the full instructions (theme, intervals, recoveries, intensity, terrain)
   are kept verbatim in the session notes and shown in Home Assistant; nothing is summarised away.
5. **Recovery and sequence**: rescheduling must respect recovery periods and the logical order
   of the week (e.g. do not put the long run the day after the VMA test if the plan separates
   them; keep the plan's order of sessions). Exact rules to define with the user; until then the
   conservative default is: keep the coach's order within the week and at least one calendar day
   between two sessions that the plan placed on different days.
6. **No invented duration**: when a session has no explicit duration (e.g. a VMA test described
   only by its content), the duration is **not guessed**; the session is imported as "duration to
   confirm" and the user is asked in Home Assistant before it can be scheduled.
7. **Competitions** mentioned in a plan are fixed events and need the user's approval (the
   sender can be spoofed; protected commitments).
8. **Updates without duplicates**: a re-sent or corrected plan for the same week replaces the
   sessions previously imported for that week (keyed by week + day/session position), instead of
   adding duplicates; sessions already done, locked or manually edited by the user are not
   overwritten silently (proposal instead).
9. **Acknowledgement**: the body asks to "confirm reception"; Tempo never replies to e-mails
   (read-only). A HA notification informs the user that a new plan was imported.

## Relation to the existing code (gap)

Commit `4bf6b74` implemented a first M5 import that parses **training lines in the e-mail body**
(`backend/src/tempo/sync/training_mail.py`, `tempo/ai/text_parser.py`). It is generic and tested
(including a real local IMAP server) but **does not match the real format**: with this coach the
body only says "Confirme la réception", so it would import nothing. It stays disabled until an
IMAP account and the authorised sender are configured. The DOCX-based import, the "duration to
confirm" state, recovery/sequence constraints and week-level update de-duplication are **not
implemented yet** (roadmap M5b).

## Open items (user)

- Provide and authorise the coach's **exact sender e-mail address** (`TEMPO_TRAINING_ALLOWED_SENDER`).
- IMAP access for the receiving mailbox (host, user, app-specific password as Docker secret).
- Confirm recovery/sequence rules (minimum rest between hard sessions, which sessions are "hard").
- Optionally provide one or two anonymised `.docx` samples as test fixtures (no real personal
  data committed without approval).
