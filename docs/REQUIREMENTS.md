# Tempo functional rules (authoritative)

## Fixed versus movable
1. University classes imported from iCal are FIXED source events. Source changes are synchronized; do not silently move other protected commitments.
2. User-defined personal STUDY work is FLEXIBLE: user enters subject, number of hours and desired completion date, potentially far in the future. **Default target date is soft** ("if possible"); allow explicit hard deadline. Study blocks can be created, moved and optimized automatically, including previously generated study blocks, unless explicitly locked or already underway.
3. Training sessions from specifically authorized emails are FLEXIBLE (day/time can move; prescribed content and duration remain). Races and competitions are FIXED. The coach's weekly plan arrives as a `.docx` attachment (weekly table, Monday-Sunday columns); suggested weekdays are not mandatory, recovery and session order must be respected, missing durations are never invented, updated plans must not create duplicates. Full specification: `docs/TRAINING_EMAILS.md`.
4. User-configured HA availability windows constrain all planning; honor fixed appointments and existing commitments.
5. Mandatory travel blocks connect locations for events, including walk, wait, transfers, buffers, and return when relevant. No impossible teleportation or overlaps.

## Study optimization
- Prefer 90-minute sessions; allow shorter sessions for remainder or tight windows.
- Avoid adjacent blocks of same subject when possible; balance subjects and distribute effort across days.
- Aim to complete total hours before soft target; if impossible, show remaining effort and propose options rather than violating hard constraints.
- Track completed minutes, remaining minutes and missed sessions; reschedule remaining effort automatically when safe.
- No fabricated completion, travel or timetable data.

## Position and routing
- Current location ALWAYS comes from configured Home Assistant phone tracker, not parent-home switching. Check freshness, accuracy and availability.
- For future trips use location of preceding event and planned event chain; if uncertain, request clarification rather than treating today's GPS as future location.
- MOTIS candidate uses Belgian Mobility feeds for TEC/SNCB/STIB/De Lijn and OSM for walking. Validate compatibility and real-time coverage. Route for arrival-by times, walking, transfers, reliability and safety margin.
- Check routes at planning, preceding day and near departure. Recalculate if live delay/cancellation threatens feasibility. Never pretend live data is available when absent.
- Allow multiple user-configurable departure reminders via HA.

## Change policy
- Reoptimize ordinary movable study and flexible training automatically within constraints.
- Ask approval to move explicitly locked items, accepted protected commitments or major user commitments.
- When iCal source changes overnight: synchronize fixed class, calculate impact, propose protected changes and wait for approval; do not block source truth update.
- Keep proposals versioned, explain differences, support approve/reject and audit actions.

## HA interface
- Home Assistant ONLY: custom integration HACS-installable with config flow, calendar, todo, sensors, service actions, conversational agent where supported, notifications and approval actions. Exact entity IDs are targets, not existing features.
- No new public Tempo frontend or separate user account system.

## Quality
- Operates 24/7 independent of Claude Code and optional LLM provider.
- Persistent DB, backup and restore, robust sync and idempotency, tested failure recovery.
- If a hard deadline is impossible, explicitly report conflicts and feasible alternatives.
