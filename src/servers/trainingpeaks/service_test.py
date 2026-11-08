from datetime import date

import pytest

from src.servers.trainingpeaks.client import TrainingPeaksClient, personal_record_types
from src.servers.trainingpeaks.conftest import ATHLETE_ID, FakeTrainingPeaksApi, load_fixture
from src.servers.trainingpeaks.service import TrainingPeaksService


def _make_sut(api: FakeTrainingPeaksApi) -> TrainingPeaksService:
    client = TrainingPeaksClient(
        auth_cookie=lambda: "fake-cookie",
        transport=api.transport,
        min_request_interval_seconds=0,
    )
    return TrainingPeaksService(client)


@pytest.mark.asyncio
async def test_list_workouts_queries_long_ranges_in_windows():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-08-10/2026-11-07",
        load_fixture("workouts"),
    )
    api.add("GET", f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-11-08/2026-11-08", [])
    sut = _make_sut(api)

    # Act
    result = await sut.list_workouts(
        date(2026, 8, 10),
        date(2026, 11, 8),
        status="all",
        sport=None,
        limit=200,
    )

    # Assert
    workout_requests = [
        request.url.path for request in api.requests if "/workouts/" in request.url.path
    ]
    assert workout_requests == [
        f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-08-10/2026-11-07",
        f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-11-08/2026-11-08",
    ]
    assert result.count == 3


@pytest.mark.asyncio
async def test_list_workouts_filters_and_totals_completed_by_sport():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-11-02/2026-11-08",
        load_fixture("workouts"),
    )
    sut = _make_sut(api)

    # Act
    result = await sut.list_workouts(
        date(2026, 11, 2),
        date(2026, 11, 8),
        status="completed",
        sport=None,
        limit=1,
    )

    # Assert
    assert result.count == 2
    assert result.truncated
    assert [workout.id for workout in result.workouts] == [1001]
    assert [(total.sport, total.count, total.distance_km) for total in result.totals] == [
        ("Bike", 1, 45.0),
        ("Run", 1, 8.45),
    ]


@pytest.mark.asyncio
async def test_list_workouts_filters_by_sport_ignoring_case():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-11-02/2026-11-08",
        load_fixture("workouts"),
    )
    sut = _make_sut(api)

    # Act
    result = await sut.list_workouts(
        date(2026, 11, 2),
        date(2026, 11, 8),
        status="all",
        sport="run",
        limit=200,
    )

    # Assert
    assert [workout.id for workout in result.workouts] == [1001]


@pytest.mark.asyncio
async def test_get_workout_merges_files_note_and_records():
    # Arrange
    api = FakeTrainingPeaksApi()
    base = f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/1001"
    api.add("GET", base, load_fixture("workout"))
    api.add("GET", f"{base}/details", load_fixture("workout_details"))
    api.add("GET", "/fitness/v6/workouts/1001/privateWorkoutNote", load_fixture("private_note"))
    api.add(
        "GET",
        f"/personalrecord/v2/athletes/{ATHLETE_ID}/workouts/1001",
        load_fixture("workout_personal_records"),
    )
    sut = _make_sut(api)

    # Act
    workout = await sut.get_workout(1001)

    # Assert
    assert workout.private_note == "Joelho incomodou no fim"
    assert [file.name for file in workout.files] == ["garmin.fit"]
    assert workout.personal_records[0].type == "speed5K"
    assert workout.personal_records[0].timeframe == "All Time"


@pytest.mark.asyncio
async def test_get_week_summary_uses_monday_to_sunday():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-11-02/2026-11-08",
        load_fixture("workouts"),
    )
    api.add(
        "POST",
        f"/fitness/v1/athletes/{ATHLETE_ID}/reporting/performancedata/2026-11-02/2026-11-08",
        load_fixture("fitness"),
    )
    api.add(
        "GET",
        f"/rx/activity/v1/workouts/calendar/{ATHLETE_ID}/2026-11-02/2026-11-08",
        load_fixture("strength_workouts"),
    )
    sut = _make_sut(api)

    # Act
    summary = await sut.get_week_summary(date(2026, 11, 5))

    # Assert
    assert (summary.start_date, summary.end_date) == (date(2026, 11, 2), date(2026, 11, 8))
    assert summary.planned_count == 3
    assert summary.completed_count == 2
    assert summary.planned_tss == pytest.approx(185.0)
    assert summary.completed_tss == pytest.approx(143.5)
    assert summary.planned_duration_hours == pytest.approx(3.25)
    assert summary.completed_duration_hours == pytest.approx(2.3)
    assert summary.fitness_at_end is not None
    assert summary.fitness_at_end.date == date(2026, 11, 8)
    assert summary.strength_workouts[0].exercises == ["Agachamento", "Remada"]


@pytest.mark.asyncio
async def test_list_personal_records_without_type_returns_best_of_each_type():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"/personalrecord/v2/athletes/{ATHLETE_ID}/Run",
        load_fixture("personal_records_speed5k"),
    )
    sut = _make_sut(api)

    # Act
    result = await sut.list_personal_records(
        "Run",
        date(2016, 1, 1),
        date(2026, 11, 8),
        pr_type=None,
        top=5,
    )

    # Assert
    record_requests = [request for request in api.requests if request.url.path.endswith("/Run")]
    assert len(record_requests) == len(personal_record_types("Run"))
    assert all(record.rank == 1 for record in result.records)
    assert len(result.records) == len(personal_record_types("Run"))


@pytest.mark.asyncio
async def test_get_fitness_reports_current_form():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "POST",
        f"/fitness/v1/athletes/{ATHLETE_ID}/reporting/performancedata/2026-11-02/2026-11-08",
        load_fixture("fitness"),
    )
    sut = _make_sut(api)

    # Act
    fitness = await sut.get_fitness(date(2026, 11, 2), date(2026, 11, 8))

    # Assert
    assert fitness.current is not None
    assert fitness.current.tsb == pytest.approx(1.6)
    assert fitness.form_status == "Neutro (treino normal)"
