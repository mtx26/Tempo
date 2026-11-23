"""Named places and exact alias resolution of free-text locations (e.g. iCal LOCATION).

Resolution is deliberately exact (case- and whitespace-insensitive): an unknown text returns
``None`` so the caller asks the user, instead of guessing a place.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Coordinates:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90 <= self.lat <= 90 or not -180 <= self.lon <= 180:
            raise ValueError("coordinates out of range")

    def as_motis(self) -> str:
        return f"{self.lat:.6f},{self.lon:.6f}"


@dataclass(frozen=True, slots=True)
class Place:
    id: str
    name: str
    coordinates: Coordinates


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(slots=True)
class PlaceDirectory:
    places: list[Place]
    aliases: dict[str, str] = field(default_factory=dict)
    """Location text -> place id, configured by the user."""
    _by_id: dict[str, Place] = field(init=False, repr=False)
    _by_alias: dict[str, Place] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_id = {p.id: p for p in self.places}
        if len(self._by_id) != len(self.places):
            raise ValueError("duplicate place id")
        missing = sorted(set(self.aliases.values()) - self._by_id.keys())
        if missing:
            raise ValueError(f"aliases point to unknown places: {missing}")
        self._by_alias = {_normalise(k): self._by_id[v] for k, v in self.aliases.items()}
        for place in self.places:
            self._by_alias.setdefault(_normalise(place.name), place)

    def get(self, place_id: str) -> Place | None:
        return self._by_id.get(place_id)

    def resolve(self, text: str | None) -> Place | None:
        if not text or not text.strip():
            return None
        return self._by_alias.get(_normalise(text))
