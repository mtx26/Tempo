"""Natural-language requests and training e-mails on a REAL PostgreSQL (LLM faked)."""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tempo.ai.llm import ExtractedItem, Extraction
from tempo.api.app import create_app
from tempo.config import Settings
from tempo.db.models import ActivityRow, Base, NotificationRow, PlaceRow, ProposalRow, StudyTaskRow
from tempo.sync.training_mail import MailMessage
from tempo.worker.jobs import training_mail_job

pytestmark = pytest.mark.integration
TOKEN = "Hq8Wm2Zr5Tx9Lb3Nc7Vd1Kf4Gs6Pj0YeUa"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeInterpreter:
    def __init__(self, extraction: Extraction | None) -> None:
        self.extraction = extraction
        self.calls: list[str] = []

    def extract(self, text: str, today: date, place_ids: list[str]) -> Extraction | None:
        self.calls.append(text)
        return self.extraction


class FakeMail:
    def __init__(self, messages: list[MailMessage]) -> None:
        self.messages = messages

    def fetch(self, since: date) -> list[MailMessage]:
        return self.messages


@pytest.fixture
def clean(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def api(database_url: str, clean: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(api_token=TOKEN, database_url=database_url))) as client:
        yield client


def say(api: TestClient, phrase: str) -> dict[str, object]:
    response = api.post("/v1/interpret", json={"text": phrase}, headers=AUTH)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def test_interpret_adds_a_soft_task_without_any_llm(
    api: TestClient, clean: sessionmaker[Session]
) -> None:
    answer = say(api, "ajoute 5h de maths pour le 20/12/2099")
    assert answer["applied"] and answer["action"] == "add" and not answer["used_llm"]
    with clean() as s:
        task = s.scalars(select(StudyTaskRow)).one()
        assert (task.subject, task.effort_minutes, task.target_kind.value) == ("maths", 300, "soft")


def test_interpret_unknown_text_without_llm_explains_supported_phrasings(api: TestClient) -> None:
    answer = say(api, "organise ma vie")
    assert not answer["applied"] and not answer["understood"]
    assert "Exemples" in str(answer["summary"])


def test_interpret_past_date_is_rejected_not_fixed(api: TestClient) -> None:
    answer = say(api, "ajoute 2h de chimie pour le 01/01/2020")
    assert not answer["applied"] and "passée" in str(answer["summary"])


def test_interpret_uses_the_llm_only_when_needed_and_validates_it(
    api: TestClient, clean: sessionmaker[Session]
) -> None:
    fake = FakeInterpreter(
        Extraction(
            items=[
                ExtractedItem(
                    kind="study_task", title="statistiques", minutes=120, day="2099-01-10"
                ),
                ExtractedItem(kind="race", title="trail", minutes=90, day="2099-01-12"),  # no time
            ]
        )
    )
    api.app.state.interpreter = fake  # type: ignore[attr-defined]
    say(api, "ajoute 1h de maths pour le 20/12/2099")
    assert fake.calls == []  # deterministic parser was enough
    answer = say(api, "il faudrait que je bosse les stats deux heures avant le 10 janvier")
    assert answer["used_llm"] and answer["applied"]
    assert "heure de départ" in str(answer["summary"])  # the race was rejected by validation
    with clean() as s:
        subjects = sorted(t.subject for t in s.scalars(select(StudyTaskRow)))
        assert subjects == ["maths", "statistiques"]
        assert s.scalars(select(ActivityRow)).first() is None


def test_training_mail_import_is_narrow_idempotent_and_protects_races(
    api: TestClient, clean: sessionmaker[Session]
) -> None:
    with clean() as s:
        s.add(PlaceRow(id="club", name="Club", latitude=50.7, longitude=4.6))
        s.commit()
    body = (
        "Salut,\n"
        "mardi 15/12/2099 : footing 40 min @club - endurance\n"
        "COURSE samedi 19/12/2099 10:00 : 10 km 60 min @club\n"
        "jeudi 17/12/2099 vers 18h je te propose du seuil 3x10'\n"
        "Bonne semaine"
    )
    message = MailMessage("<m1@coach>", "coach@club.be", "Programme", body)
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime(2099, 12, 14, 8, tzinfo=UTC)
    tz = ZoneInfo("Europe/Brussels")
    stats = training_mail_job(clean, FakeMail([message]), now, tz, None)
    assert stats == {"trainings": 1, "proposals": 1, "unparsed": 1}
    assert training_mail_job(clean, FakeMail([message]), now, tz, None)["trainings"] == 0
    with clean() as s:
        trainings = list(s.scalars(select(ActivityRow)))
        assert len(trainings) == 1 and trainings[0].notes == "endurance"
        assert trainings[0].source == "email" and trainings[0].place_id == "club"
        kinds = sorted(n.kind for n in s.scalars(select(NotificationRow)))
        assert kinds == ["training_added", "training_unparsed"]
    proposals = api.get("/v1/proposals", headers=AUTH).json()
    assert len(proposals) == 1 and proposals[0]["kind"] == "training_import"
    assert api.post(f"/v1/proposals/{proposals[0]['id']}/approve", headers=AUTH).status_code == 200
    with clean() as s:
        race = s.scalars(select(ActivityRow).where(ActivityRow.kind == "race")).one()
        assert race.earliest_start_at.astimezone(tz).hour == 10
        assert race.source == "email_llm"


def test_training_mail_llm_items_become_proposals(clean: sessionmaker[Session]) -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    fake = FakeInterpreter(
        Extraction(
            items=[
                ExtractedItem(
                    kind="training",
                    title="Seuil",
                    minutes=50,
                    day="2099-12-17",
                    start_time="18:00",
                    notes="3 x 10 min seuil",
                ),
                ExtractedItem(kind="study_task", title="ignored", minutes=60, day="2099-12-18"),
            ]
        )
    )
    message = MailMessage(
        "<m2@coach>", "coach@club.be", "Jeudi", "jeudi 17/12 vers 18h seuil 3x10'"
    )
    now = datetime(2099, 12, 14, 8, tzinfo=UTC)
    stats = training_mail_job(clean, FakeMail([message]), now, ZoneInfo("Europe/Brussels"), fake)
    assert stats["proposals"] == 1 and stats["trainings"] == 0
    with clean() as s:
        proposal = s.scalars(select(ProposalRow)).one()
        assert proposal.details["origin"] == "ai"
        assert proposal.details["result"]["tasks"] == []
        assert s.scalars(select(ActivityRow)).first() is None  # nothing applied before approval
