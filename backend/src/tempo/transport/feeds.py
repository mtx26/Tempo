"""Registry of the Belgian public transport feeds used by Tempo, and the polling quota budget.

Every URL below was fetched successfully on 2026-11-23 (anonymous access, HTTP 200, protobuf
for realtime, ZIP for static) — see ARCHITECTURE.md "Belgian feeds". Nothing here is guessed:

- Two hosts, same paths (``PORTAL_HOSTS``, the only hosts that ever receive the key):
  anonymous ``ANONYMOUS_HOST``; with a key ``KEYED_HOST``, the host of the developer portal's
  "Try it" requests for the Standard subscription (``Feed.url_for``). On 2026-11-25 both
  answered 200 for every feed with a valid, a wrong or no key: quota attribution to the
  subscription is visible only in the developer portal reports.
- Realtime is JSON by default; ``?format=protobuf`` is mandatory (the JSON transcoding mangles
  int64 fields). Trip updates use ``unroll=off`` (portal parameter, both hosts): stops without
  a prediction are omitted, realtime data identical, ~30 % smaller.
- Quotas come from the portal terms §5 (indicative): anonymous 100 requests/day and 10/minute;
  free Standard account 12 000/day and 500/minute; beyond them the portal may answer HTTP 429.
  The key travels in header ``bmc-partner-key`` (portal FAQ and "Try it" requests).
- STIB-MIVB publishes no GTFS-RT: MOTIS routes STIB on its static GTFS.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

ANONYMOUS_HOST = "api-management-discovery-production.azure-api.net"
KEYED_HOST = "api-management-opendata-production.azure-api.net"
PORTAL_HOSTS = frozenset({ANONYMOUS_HOST, KEYED_HOST})
FEED_PATH = "/api/gtfs/feed"
KEY_HEADER = "bmc-partner-key"
SECONDS_PER_DAY = 86_400


class Operator(StrEnum):
    """Belgian Mobility operator identifiers (path segment of the feed URLs)."""

    SNCB = "nmbssncb"
    TEC = "tec"
    DE_LIJN = "delijn"
    STIB = "stibmivb"


class FeedKind(StrEnum):
    STATIC = "gtfs-static"
    TRIP_UPDATES = "gtfs-rt-trip-updates"
    SERVICE_ALERTS = "gtfs-rt-alerts"


class AccessTier(StrEnum):
    ANONYMOUS = "anonymous"
    STANDARD = "standard"


@dataclass(frozen=True, slots=True)
class Quota:
    per_day: int
    per_minute: int


QUOTAS: Mapping[AccessTier, Quota] = {
    AccessTier.ANONYMOUS: Quota(per_day=100, per_minute=10),
    AccessTier.STANDARD: Quota(per_day=12_000, per_minute=500),
}
SAFETY_FRACTION = 0.8
"""Plan at most 80 % of a quota: the terms say limits may change at any time."""


@dataclass(frozen=True, slots=True)
class Feed:
    operator: Operator
    kind: FeedKind
    path: str
    """Below ``FEED_PATH``, identical on both hosts."""
    publisher_interval: timedelta
    """How often the publisher refreshes the feed (portal catalogue)."""
    needs_repair: bool = False
    """True when Tempo must sanitise the feed before MOTIS can use it (see ``gtfsrt``)."""

    def url_for(self, tier: "AccessTier") -> str:
        host = ANONYMOUS_HOST if tier is AccessTier.ANONYMOUS else KEYED_HOST
        return f"https://{host}{FEED_PATH}/{self.path}"

    @property
    def url(self) -> str:
        """Anonymous URL."""
        return self.url_for(AccessTier.ANONYMOUS)

    @property
    def is_realtime(self) -> bool:
        return self.kind is not FeedKind.STATIC

    @property
    def name(self) -> str:
        return f"{self.operator.value}/{self.kind.value}"


_DAILY = timedelta(days=1)
_RT = timedelta(seconds=30)


def _static(operator: Operator) -> Feed:
    return Feed(operator, FeedKind.STATIC, f"{operator.value}/static", _DAILY)


def _rt(operator: Operator, kind: FeedKind, path: str, *, repair: bool = False) -> Feed:
    query = "format=protobuf&unroll=off" if kind is FeedKind.TRIP_UPDATES else "format=protobuf"
    return Feed(operator, kind, f"{operator.value}/rt/{path}?{query}", _RT, needs_repair=repair)


FEEDS: tuple[Feed, ...] = (
    _static(Operator.TEC),
    _static(Operator.SNCB),
    _static(Operator.DE_LIJN),
    _static(Operator.STIB),
    # TEC trip descriptors carry start_time shifted by 12 h for afternoon trips (observed
    # 2026-11-23: "08:05:00" for a trip whose GTFS departure is 20:05:00): ~50 % unresolvable.
    _rt(Operator.TEC, FeedKind.TRIP_UPDATES, "trip-update", repair=True),
    _rt(Operator.SNCB, FeedKind.TRIP_UPDATES, "trip-update"),
    _rt(Operator.DE_LIJN, FeedKind.TRIP_UPDATES, "trip-update"),
    _rt(Operator.TEC, FeedKind.SERVICE_ALERTS, "alert", repair=True),
    _rt(Operator.SNCB, FeedKind.SERVICE_ALERTS, "alert"),
    _rt(Operator.DE_LIJN, FeedKind.SERVICE_ALERTS, "alert"),
)


def feed(operator: Operator, kind: FeedKind) -> Feed:
    for candidate in FEEDS:
        if candidate.operator is operator and candidate.kind is kind:
            return candidate
    raise KeyError(f"no {kind.value} feed for {operator.value}")


@dataclass(frozen=True, slots=True)
class PollingPlan:
    """How often each feed kind is fetched from the portal (one request per fetch).

    Default = Standard tier: trip updates every minute (publisher refresh: 30 s), alerts every
    5 minutes. All six realtime feeds: 5 184 requests/day, 43 % of the 12 000/day quota and
    54 % of the 80 % planning cap, leaving room for static downloads, retries and quota changes;
    peak 6 requests in a minute (quota 500)."""

    trip_updates: timedelta = timedelta(minutes=1)
    service_alerts: timedelta = timedelta(minutes=5)
    static: timedelta = _DAILY

    def interval(self, kind: FeedKind) -> timedelta:
        return {
            FeedKind.TRIP_UPDATES: self.trip_updates,
            FeedKind.SERVICE_ALERTS: self.service_alerts,
            FeedKind.STATIC: self.static,
        }[kind]


class QuotaExceededError(ValueError):
    """The polling plan would exceed the portal quota of the access tier."""


@dataclass(frozen=True, slots=True)
class Budget:
    requests_per_day: int
    requests_per_minute_peak: int
    allowed_per_day: int
    """``SAFETY_FRACTION`` of the tier's daily quota."""

    @property
    def fits(self) -> bool:
        return self.requests_per_day <= self.allowed_per_day


def budget(feeds: Iterable[Feed], plan: PollingPlan, tier: AccessTier) -> Budget:
    """Requests/day needed by ``plan`` for ``feeds``; the peak minute assumes every feed is
    due at the same time (worst case)."""
    selected = list(feeds)
    per_day = 0
    for item in selected:
        seconds = plan.interval(item.kind).total_seconds()
        if seconds < item.publisher_interval.total_seconds() or seconds <= 0:
            raise ValueError(f"{item.name}: polling faster than the publisher refreshes it")
        per_day += -(-SECONDS_PER_DAY // int(seconds))  # ceil
    quota = QUOTAS[tier]
    return Budget(
        requests_per_day=per_day,
        requests_per_minute_peak=len(selected),
        allowed_per_day=int(quota.per_day * SAFETY_FRACTION),
    )


def check_budget(feeds: Iterable[Feed], plan: PollingPlan, tier: AccessTier) -> Budget:
    selected = list(feeds)
    result = budget(selected, plan, tier)
    quota = QUOTAS[tier]
    if not result.fits:
        raise QuotaExceededError(
            f"{result.requests_per_day} requests/day exceed {result.allowed_per_day} "
            f"({int(SAFETY_FRACTION * 100)} % of the {tier.value} quota {quota.per_day}/day)"
        )
    if result.requests_per_minute_peak > quota.per_minute:
        raise QuotaExceededError(
            f"{result.requests_per_minute_peak} simultaneous requests exceed "
            f"{quota.per_minute}/minute ({tier.value})"
        )
    return result
