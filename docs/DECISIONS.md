# Architecture decisions

- D001: Home Assistant is the ONLY user-facing interface; custom HACS-compatible integration. No independent frontend.
- D002: Python/FastAPI + PostgreSQL + worker + OR-Tools CP-SAT are baseline candidates; benchmark before locking production.
- D003: MOTIS is preferred routing candidate; benchmark against OpenTripPlanner with Belgian multi-operator and realtime data.
- D004: Belgian Mobility Open Data replaces TEC-only feed assumptions. Verify actual coverage, access terms and STIB realtime adapter.
- D005: Actual location always from configured HA phone tracker; future route origin derives from planned sequence where applicable.
- D006: University iCal timetable fixed; personal study blocks movable. Study target dates soft by default, hard only if explicitly stated.
- D007: All training movable; races/competitions fixed.
- D008: Protected changes require approval; ordinary flexible planning can change automatically.
- D009: No destructive/server deployment without user approval; keep secrets out of repository.
