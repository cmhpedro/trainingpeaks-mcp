from datetime import date

import pytest

from src.servers.trainingpeaks.client import (
    TrainingPeaksAuthError,
    TrainingPeaksClient,
    TrainingPeaksNotFoundError,
)
from src.servers.trainingpeaks.conftest import ATHLETE_ID, FakeTrainingPeaksApi, load_fixture

_WORKOUTS_PATH = f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/2026-11-02/2026-11-08"
_WORKOUT_PATH = f"/fitness/v6/athletes/{ATHLETE_ID}/workouts/1001"


def _make_sut(api: FakeTrainingPeaksApi) -> TrainingPeaksClient:
    return TrainingPeaksClient(
        auth_cookie=lambda: "fake-cookie",
        transport=api.transport,
        min_request_interval_seconds=0,
    )


@pytest.mark.asyncio
async def test_list_workouts_exchanges_cookie_for_token_and_converts_units():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add("GET", _WORKOUTS_PATH, load_fixture("workouts"))
    sut = _make_sut(api)

    # Act
    workouts = await sut.list_workouts(date(2026, 11, 2), date(2026, 11, 8))

    # Assert
    token_request = api.requests_to("/users/v3/token")[0]
    assert token_request.headers["Cookie"] == "Production_tpAuth=fake-cookie"
    workout_request = api.requests_to(_WORKOUTS_PATH)[0]
    assert workout_request.headers["Authorization"] == "Bearer fake-access-token"

    run, bike, swim = workouts
    assert run.sport == "Run"
    assert run.completed
    assert run.duration_min == pytest.approx(48.0)
    assert run.distance_km == pytest.approx(8.45)
    assert run.tss == pytest.approx(48.4)
    assert run.average_heart_rate_bpm == 142
    assert run.average_pace_per_km == "5:41"
    assert run.average_pace_per_100m is None
    assert bike.sport == "Bike"
    assert bike.average_speed_kmh == pytest.approx(30.0)
    assert bike.normalized_power_w == 228
    assert bike.average_pace_per_km is None
    assert swim.sport == "Swim"
    assert not swim.completed
    assert swim.duration_planned_min == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_token_is_reused_between_requests():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add("GET", _WORKOUTS_PATH, [])
    sut = _make_sut(api)

    # Act
    await sut.list_workouts(date(2026, 11, 2), date(2026, 11, 8))
    await sut.list_workouts(date(2026, 11, 2), date(2026, 11, 8))

    # Assert
    assert len(api.requests_to("/users/v3/token")) == 1
    assert len(api.requests_to("/users/v3/user")) == 1


@pytest.mark.asyncio
async def test_expired_cookie_raises_auth_error():
    # Arrange
    api = FakeTrainingPeaksApi(token_statuses=[401])
    sut = _make_sut(api)

    # Act
    with pytest.raises(TrainingPeaksAuthError) as error:
        await sut.get_account()

    # Assert
    assert "TRAININGPEAKS_AUTH_COOKIE" in str(error.value)


@pytest.mark.asyncio
async def test_missing_cookie_raises_auth_error_without_calling_api():
    # Arrange
    api = FakeTrainingPeaksApi()
    sut = TrainingPeaksClient(auth_cookie=lambda: "", transport=api.transport)

    # Act
    with pytest.raises(TrainingPeaksAuthError):
        await sut.get_account()

    # Assert
    assert api.requests == []


@pytest.mark.asyncio
async def test_unauthorized_request_renews_token_once():
    # Arrange
    api = FakeTrainingPeaksApi()
    sut = _make_sut(api)
    await sut.get_account()
    api.add("GET", _WORKOUTS_PATH, None, status=401)

    # Act
    with pytest.raises(TrainingPeaksAuthError):
        await sut.list_workouts(date(2026, 11, 2), date(2026, 11, 8))

    # Assert
    assert len(api.requests_to(_WORKOUTS_PATH)) == 2
    assert len(api.requests_to("/users/v3/token")) == 2


@pytest.mark.asyncio
async def test_get_workout_decodes_structure_and_comments():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add("GET", _WORKOUT_PATH, load_fixture("workout"))
    sut = _make_sut(api)

    # Act
    workout = await sut.get_workout(1001)

    # Assert
    assert workout.has_private_note
    assert workout.intensity_factor_planned == pytest.approx(0.75)
    assert workout.maximum_speed_kmh == pytest.approx(14.8)
    assert workout.comments[0].author == "Treinador X"
    assert workout.comments[0].is_coach
    assert isinstance(workout.structure, dict)
    assert workout.structure["primaryIntensityMetric"] == "percentOfThresholdHr"
    # compact_json tira os `null` da estrutura
    assert "notes" not in str(workout.structure)


@pytest.mark.asyncio
async def test_get_missing_workout_raises_not_found():
    # Arrange
    api = FakeTrainingPeaksApi()
    sut = _make_sut(api)

    # Act / Assert
    with pytest.raises(TrainingPeaksNotFoundError):
        await sut.get_workout(999)


@pytest.mark.asyncio
async def test_list_personal_records_derives_time_and_pace_for_speed():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"/personalrecord/v2/athletes/{ATHLETE_ID}/Run",
        load_fixture("personal_records_speed5k"),
    )
    sut = _make_sut(api)

    # Act
    records = await sut.list_personal_records("Run", "speed5K", date(2016, 1, 1), date(2026, 11, 8))

    # Assert
    best = records[0]
    assert best.kind == "speed"
    assert best.time == "25:00"
    assert best.pace_per_km == "5:00"
    assert best.date == date(2025, 5, 10)
    request = api.requests[-1]
    assert request.url.params["prType"] == "speed5K"


@pytest.mark.asyncio
async def test_list_fitness_posts_to_read_only_report():
    # Arrange
    api = FakeTrainingPeaksApi()
    path = f"/fitness/v1/athletes/{ATHLETE_ID}/reporting/performancedata/2026-11-02/2026-11-08"
    api.add("POST", path, load_fixture("fitness"))
    sut = _make_sut(api)

    # Act
    days = await sut.list_fitness(date(2026, 11, 2), date(2026, 11, 8))

    # Assert
    assert [day.date for day in days] == [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 8)]
    assert days[0].ctl == pytest.approx(50.0)
    assert days[0].tsb == pytest.approx(-8.3)


@pytest.mark.asyncio
async def test_get_workout_analysis_without_device_file_returns_empty_channels():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "POST",
        "/workout-analysis/v2/analyze/summary",
        {
            "startTimestamp": "2026-11-02T06:30:00",
            "data": {"TotalDistance": {"friendlyName": "Distance", "value": 8450.5, "unit": "m"}},
        },
    )
    sut = _make_sut(api)

    # Act
    analysis = await sut.get_workout_analysis(1001)

    # Assert
    assert analysis.totals[0].name == "Distance"
    assert analysis.channels == []
    assert analysis.laps == []
    request = api.requests_to("/workout-analysis/v2/analyze/summary")[0]
    assert request.url.host == "api.peakswaresb.com"
    assert request.headers["Origin"] == "https://app.trainingpeaks.com"


@pytest.mark.asyncio
async def test_list_workout_files_of_planned_workout_accepts_null_lists():
    # Arrange
    api = FakeTrainingPeaksApi()
    api.add(
        "GET",
        f"{_WORKOUT_PATH}/details",
        {"workoutDeviceFileInfos": None, "attachmentFileInfos": None},
    )
    sut = _make_sut(api)

    # Act
    files = await sut.list_workout_files(1001)

    # Assert
    assert files == []
