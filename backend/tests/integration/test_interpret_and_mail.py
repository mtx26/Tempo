"""Natural-language requests and training e-mails on a REAL PostgreSQL (LLM faked)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("Europe/Brussels")
NOW = datetime.now(UTC)
TODAY = NOW.astimezone(TZ).date()


def ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


def dmy(day: date) -> str:
    return day.strftime("%d/%m/%Y")


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
    answer = say(api, f"ajoute 5h de maths pour le {dmy(ahead(20))}")
    assert answer["applied"] and answer["action"] == "add" and not answer["used_llm"]
    with clean() as s:
        task = s.scalars(select(StudyTaskRow)).one()
        assert (task.subject, task.effort_minutes, task.target_kind.value) == ("maths", 300, "soft")


def test_interpret_unknown_text_without_llm_explains_supported_phrasings(api: TestClient) -> None:
    answer = say(api, "organise ma vie")
    assert not answer["applied"] and not answer["understood"]
    assert "Exemples" in str(answer["summary"])


def test_interpret_past_or_far_dates_are_rejected_not_fixed(api: TestClient) -> None:
    assert "passée" in str(say(api, "ajoute 2h de chimie pour le 01/01/2020")["summary"])
    far = say(api, f"ajoute 2h de chimie pour le {dmy(ahead(400))}")
    assert not far["applied"] and "plus d'un an" in str(far["summary"])


def test_interpret_uses_the_llm_only_when_needed_and_validates_it(
    api: TestClient, clean: sessionmaker[Session]
) -> None:
    fake = FakeInterpreter(
        Extraction(
            items=[
                ExtractedItem(
                    kind="study_task", title="statistiques", minutes=120, day=ahead(10).isoformat()
                ),
                ExtractedItem(kind="race", title="trail", minutes=90, day=ahead(12).isoformat()),
            ]
        )
    )
    api.app.state.interpreter = fake  # type: ignore[attr-defined]
    say(api, f"ajoute 1h de maths pour le {dmy(ahead(20))}")
    assert fake.calls == []  # deterministic parser was enough
    answer = say(api, "il faudrait que je bosse les stats deux heures avant dix jours")
    assert answer["used_llm"] and answer["applied"]
    assert "heure de départ" in str(answer["summary"])  # the race was rejected by validation
    with clean() as s:
        subjects = sorted(t.subject for t in s.scalars(select(StudyTaskRow)))
        assert subjects == ["maths", "statistiques"]
        assert s.scalars(select(ActivityRow)).first() is None


def test_too_many_items_are_refused(api: TestClient) -> None:
    lines = "\n".join(f"ajoute 1h de sujet{i} pour le {dmy(ahead(30))}" for i in range(21))
    answer = say(api, lines)
    assert not answer["applied"] and "plus de 20" in str(answer["summary"])


def test_training_mail_import_is_narrow_idempotent_and_protects_races(
    api: TestClient, clean: sessionmaker[Session]
) -> None:
    with clean() as s:
        s.add(PlaceRow(id="club", name="Club", latitude=50.7, longitude=4.6))
        s.commit()
    body = (
        "Salut,\n"
        f"{dmy(ahead(3))} : footing 40 min @club - endurance\n"
        f"COURSE {dmy(ahead(7))} 10:00 : 10 km 60 min @club\n"
        f"{dmy(ahead(5))} vers 18h je te propose du seuil 3x10'\n"
        "Bonne semaine"
    )
    message = MailMessage("<m1@coach>", "coach@club.be", "Programme", body)
    stats = training_mail_job(clean, FakeMail([message]), NOW, TZ, None)
    assert stats == {"trainings": 1, "proposals": 1, "unparsed": 1, "failed": 0}
    again = training_mail_job(clean, FakeMail([message]), NOW, TZ, None)
    assert again == {"trainings": 0, "proposals": 0, "unparsed": 0, "failed": 0}  # seen
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
        assert race.earliest_start_at.astimezone(TZ).hour == 10
        assert race.source == "email_llm"


def test_llm_is_not_called_again_for_a_seen_message(clean: sessionmaker[Session]) -> None:
    fake = FakeInterpreter(None)
    message = MailMessage("<m3@coach>", "coach@club.be", "x", f"{dmy(ahead(2))} vers 18h seuil")
    training_mail_job(clean, FakeMail([message]), NOW, TZ, fake)
    training_mail_job(clean, FakeMail([message]), NOW, TZ, fake)
    assert len(fake.calls) == 1


def test_far_or_broken_dates_in_mail_do_not_block_other_messages(
    clean: sessionmaker[Session],
) -> None:
    bad = MailMessage("<bad@coach>", "coach@club.be", "x", "lundi 31/12/9999 : footing 45 min")
    good = MailMessage("<ok@coach>", "coach@club.be", "y", f"{dmy(ahead(2))} : footing 30 min")
    stats = training_mail_job(clean, FakeMail([bad, good]), NOW, TZ, None)
    assert stats["trainings"] == 1
    with clean() as s:
        assert any(n.kind == "training_rejected" for n in s.scalars(select(NotificationRow)))


def test_training_mail_llm_items_become_proposals(clean: sessionmaker[Session]) -> None:
    fake = FakeInterpreter(
        Extraction(
            items=[
                ExtractedItem(
                    kind="training",
                    title="Seuil",
                    minutes=50,
                    day=ahead(4).isoformat(),
                    start_time="18:00",
                    notes="3 x 10 min seuil",
                ),
                ExtractedItem(
                    kind="study_task", title="ignored", minutes=60, day=ahead(5).isoformat()
                ),
            ]
        )
    )
    message = MailMessage("<m2@coach>", "coach@club.be", "Jeudi", f"{dmy(ahead(4))} vers 18h seuil")
    stats = training_mail_job(clean, FakeMail([message]), NOW, TZ, fake)
    assert stats["proposals"] == 1 and stats["trainings"] == 0
    with clean() as s:
        proposal = s.scalars(select(ProposalRow)).one()
        assert proposal.details["origin"] == "ai"
        assert proposal.details["result"]["tasks"] == []
        assert s.scalars(select(ActivityRow)).first() is None  # nothing applied before approval
