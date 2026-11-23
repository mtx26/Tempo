# Graph Report - Tempo  (2026-11-23)

## Corpus Check
- 63 files · ~44,197 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 13 file(s) not represented in the graph (top: (none) 5, .pbf 4, .graphify-bak 1)

## Summary
- 912 nodes · 2618 edges · 42 communities (26 shown, 16 thin omitted)
- Extraction: 82% EXTRACTED · 18% INFERRED · 0% AMBIGUOUS · INFERRED: 462 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `8e8f0f1c`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- rt_relay.py
- solver.py
- Journey
- pytest
- travel.py
- test_scheduler.py
- test_alerts.py
- test_ics.py
- ha.py
- ics.py
- test_motis.py
- quality
- belgium.yml
- ics_repository.py
- test_api_ha.py
- brussels.yml
- test_persistence.py
- MotisClient
- Leg
- conftest.py
- motis.py
- app.py
- FixedEvent
- test_motis_belgium_live.py
- test_api.py
- db/models.py
- routing.py
- sqlalchemy
- graphify
- tempo-backend

## God Nodes (most connected - your core abstractions)
1. `Journey` - 39 edges
2. `plan_day_travel()` - 34 edges
3. `Operator` - 33 edges
4. `Interval` - 32 edges
5. `parse_ics()` - 30 edges
6. `FeedKind` - 28 edges
7. `problem()` - 28 edges
8. `Coordinates` - 26 edges
9. `demand()` - 26 edges
10. `solve_valid()` - 26 edges

## Surprising Connections (you probably didn't know these)
- `test_only_tec_is_repaired()` --uses--> `Operator`  [INFERRED]
  backend/tests/test_feeds.py → backend/src/tempo/transport/feeds.py
- `FeedRelay` --uses--> `InvalidFeedError`  [INFERRED]
  backend/src/tempo/transport/rt_relay.py → backend/src/tempo/transport/gtfsrt.py
- `_Entry` --uses--> `FeedSummary`  [INFERRED]
  backend/src/tempo/transport/rt_relay.py → backend/src/tempo/transport/gtfsrt.py
- `MotisClient` --uses--> `Journey`  [INFERRED]
  backend/src/tempo/transport/motis.py → backend/src/tempo/transport/routing.py
- `motis()` --uses--> `MotisClient`  [INFERRED]
  backend/tests/integration/test_motis_belgium_live.py → backend/src/tempo/transport/motis.py

## Import Cycles
- None detected.

## Communities (42 total, 16 thin omitted)

### Community 0 - "rt_relay.py"
Cohesion: 0.06
Nodes (58): AccessTier, Budget, budget(), check_budget(), feed(), Feed, FeedKind, Operator (+50 more)

### Community 1 - "solver.py"
Cohesion: 0.07
Nodes (41): _require_aware(), TargetKind, PlannedBlock, PlanningProblem, PlanResult, StudyDemand, utc(), _Block (+33 more)

### Community 2 - "Journey"
Cohesion: 0.07
Nodes (51): assess_position(), DeclaredStay, infer_origin(), LocatedEvent, Origin, OriginSource, PhonePosition, PositionStatus (+43 more)

### Community 3 - "pytest"
Cohesion: 0.05
Nodes (46): Settings, StudyTask, check_fresh(), decode(), drop_start_times(), FeedSummary, InvalidFeedError, prepare() (+38 more)

### Community 4 - "travel.py"
Cohesion: 0.14
Nodes (34): Place, RoutingProvider, _arrive_by(), _CachedRouting, _depart_after(), _journey_warnings(), _label(), _outbound() (+26 more)

### Community 5 - "test_scheduler.py"
Cohesion: 0.19
Nodes (34): Interval, PlanStatus, Preferences, at(), demand(), end_of(), minutes(), problem() (+26 more)

### Community 6 - "test_alerts.py"
Cohesion: 0.11
Nodes (18): Fixed, journey(), lecture(), real_journeys(), test_alerts_break_ties_but_never_exclude_a_journey(), test_alerts_outside_their_impact_period_are_not_relevant(), test_alerts_without_period_stay_relevant_and_are_deduplicated(), test_informational_alerts_and_small_delays_are_not_warned() (+10 more)

### Community 7 - "test_ics.py"
Cohesion: 0.17
Nodes (27): IcsParseError, parse_ics(), by_summary(), _calendar(), _event(), parse(), _single(), test_all_day_events_are_reported_as_skipped_not_blocking() (+19 more)

### Community 8 - "ha.py"
Cohesion: 0.16
Nodes (12): _audit(), create_task(), get_session(), list_tasks(), _out(), push_location(), update_task(), PositionIn (+4 more)

### Community 9 - "ics.py"
Cohesion: 0.16
Nodes (10): _as_aware(), _build_event(), _import_duplicates(), _recurrence_key(), _scan_sources(), _Skipped, _source_problem(), _SourceScan (+2 more)

### Community 10 - "test_motis.py"
Cohesion: 0.15
Nodes (14): client(), ok(), test_direct_walking_route_is_kept_as_a_valid_option(), test_http_errors_raise_routing_unavailable(), test_invalid_base_url_is_rejected(), test_itineraries_are_parsed_with_realtime_flags(), test_itinerary_with_cancelled_leg_is_excluded(), test_malformed_response_raises_instead_of_partial_data() (+6 more)

### Community 11 - "quality"
Cohesion: 0.08
Nodes (25): run, POSTGRES_PASSWORD, TEMPO_TEST_DATABASE_URL, jobs, quality, name, on, pull_request (+17 more)

### Community 12 - "belgium.yml"
Cohesion: 0.08
Nodes (25): delijn, sncb, stib, tec, path, rt, geocoding, osm (+17 more)

### Community 13 - "ics_repository.py"
Cohesion: 0.21
Nodes (17): active_events(), _apply(), apply_ics_sync(), _changed_fields(), _ChangeLog, _find(), get_or_create_source(), _is_suspect() (+9 more)

### Community 14 - "test_api_ha.py"
Cohesion: 0.20
Nodes (14): PhonePositionRow, api(), create_task(), test_created_task_has_soft_target_by_default(), test_hard_deadline_must_be_explicit(), test_invalid_positions_are_rejected(), test_invalid_tasks_are_rejected(), test_older_position_does_not_overwrite_newer() (+6 more)

### Community 15 - "brussels.yml"
Cohesion: 0.10
Nodes (20): sncb, stib, geocoding, osm, osr_footpath, reverse_geocoding, url, server (+12 more)

### Community 16 - "test_persistence.py"
Cohesion: 0.21
Nodes (14): sync(), test_audit_lists_changed_occurrences_without_event_text(), test_concurrent_migrations_on_fresh_database_succeed(), test_empty_feed_is_suspect_and_deletes_nothing(), test_event_moved_beyond_window_is_changed_not_removed(), test_first_sync_inserts_every_occurrence(), test_identical_resync_changes_nothing(), test_invalid_source_event_does_not_delete_stored_occurrence() (+6 more)

### Community 17 - "MotisClient"
Cohesion: 0.20
Nodes (11): MotisClient, RoutingUnavailableError, motis(), test_arrive_by_journeys_respect_the_deadline(), test_day_travel_is_routed_end_to_end_with_real_motis(), test_depart_at_journeys_do_not_leave_before_the_query(), test_destination_outside_the_region_yields_no_journey(), test_health_endpoint_is_up() (+3 more)

### Community 18 - "Leg"
Cohesion: 0.13
Nodes (4): _aware_utc(), Leg, ServiceAlert, test_alert_period_overlap_rules()

### Community 19 - "conftest.py"
Cohesion: 0.16
Nodes (7): alembic_config(), upgrade_to_head(), database_url(), downgrade_to_base(), engine(), session(), test_migrations_are_reversible()

### Community 20 - "motis.py"
Cohesion: 0.31
Nodes (6): _alert(), _error_text(), _journey(), _leg(), _optional_time(), _time()

### Community 22 - "FixedEvent"
Cohesion: 0.22
Nodes (8): FixedEvent, diff_events(), EventDiff, EventKey, _index(), _overlaps(), Window, test_resync_of_identical_feed_is_a_no_op()

### Community 23 - "test_motis_belgium_live.py"
Cohesion: 0.24
Nodes (6): motis(), test_alerts_are_parsed_when_realtime_alerts_are_loaded(), test_cross_country_arrive_by_respects_the_deadline(), test_each_regional_operator_is_routed(), test_stib_legs_have_no_realtime(), tomorrow_at()

### Community 24 - "test_api.py"
Cohesion: 0.29
Nodes (9): make_client(), test_health_is_public_and_reports_ok(), test_lowercase_bearer_scheme_is_accepted(), test_openapi_schema_is_not_exposed(), test_status_rejects_non_bearer_scheme(), test_status_rejects_wrong_token(), test_status_requires_token(), test_status_with_valid_token_returns_version_and_timezone() (+1 more)

### Community 25 - "db/models.py"
Cohesion: 0.25
Nodes (5): Base, _enum(), ScheduleVersion, StudyBlockRow, EventStatus

### Community 27 - "sqlalchemy"
Cohesion: 0.33
Nodes (3): downgrade(), upgrade(), upgrade()

## Knowledge Gaps
- **2 isolated node(s):** `C:\Users\mtx_2\.local\bin\graphify-mcp.exe`, `tempo-backend`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 235 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **16 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Journey` connect `Journey` to `travel.py`, `test_alerts.py`, `MotisClient`, `Leg`, `motis.py`, `routing.py`?**
  _High betweenness centrality (0.040) - this node is a cross-community bridge._
- **Are the 19 inferred relationships involving `Journey` (e.g. with `MotisClient` and `_arrive_by()`) actually correct?**
  _`Journey` has 19 INFERRED edges - model-reasoned connections that need verification._
- **What connects `C:\Users\mtx_2\.local\bin\graphify-mcp.exe`, `tempo-backend` to the rest of the system?**
  _2 weakly-connected nodes found - possible missing edges._
- **Should `rt_relay.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05759429153924567 - nodes in this community are weakly interconnected._
- **Why does `TargetKind` connect `solver.py` to `pytest`, `travel.py`, `test_scheduler.py`, `ha.py`, `test_persistence.py`, `db/models.py`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Are the 21 inferred relationships involving `Operator` (e.g. with `FeedRelay` and `test_de_lijn_trip_updates_are_live()`) actually correct?**
  _`Operator` has 21 INFERRED edges - model-reasoned connections that need verification._
- **Should `solver.py` be split into smaller, more focused modules?**
  _Cohesion score 0.07105538140020899 - nodes in this community are weakly interconnected._