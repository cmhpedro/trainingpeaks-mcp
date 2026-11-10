import asyncio
import json
import re
import time
from collections.abc import Callable
from datetime import date, datetime

from httpx import AsyncBaseTransport, AsyncClient, HTTPStatusError, Response
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue, model_validator

from src.lib.http import AutoRaiseTransport, ReadOnlyTransport, is_retryable_http_error
from src.lib.retry import retry
from src.lib.units import (
    format_duration,
    format_pace,
    hours_to_minutes,
    meters_to_km,
    mps_to_kmh,
    round_or_none,
)
from src.lib.utils import compact_json
from src.log import logger
from src.servers.trainingpeaks.domain import (
    AccountModel,
    AnalysisChannelModel,
    AnalysisLapColumnModel,
    AnalysisTotalModel,
    AnalysisZoneModel,
    AthleteSettingsModel,
    AtpWeekModel,
    AvailabilityModel,
    CalendarNoteModel,
    EquipmentKind,
    EquipmentModel,
    EventModel,
    FitnessDayModel,
    HealthMetricEntryModel,
    LibraryWorkoutModel,
    MetricValueModel,
    NoteCommentModel,
    PersonalRecordKind,
    PersonalRecordModel,
    PersonalRecordSport,
    PoolLengthModel,
    StrengthBlockModel,
    StrengthExerciseModel,
    StrengthSetModel,
    StrengthWorkoutModel,
    StrengthWorkoutSummaryModel,
    WorkoutAnalysisModel,
    WorkoutCommentModel,
    WorkoutDetailModel,
    WorkoutFileModel,
    WorkoutLibraryModel,
    WorkoutPersonalRecordModel,
    WorkoutSummaryModel,
    ZoneModel,
    ZoneSetModel,
)

_API_URL = "https://tpapi.trainingpeaks.com"
# Análise de treino e treinos de força moram na API da Peaksware (mesmo token)
_PEAKSWARE_URL = "https://api.peakswaresb.com"
_APP_ORIGIN = "https://app.trainingpeaks.com"
_AUTH_COOKIE_NAME = "Production_tpAuth"

# Endpoints que usam POST só para consultar. Qualquer outra escrita é
# bloqueada no transport, antes de sair da máquina.
_READ_ONLY_POST_PATHS = (
    re.compile(r"/fitness/v1/athletes/\d+/reporting/performancedata/[\d-]+/[\d-]+"),
    re.compile(r"/workout-analysis/v2/analyze/(summary|charts|laps)"),
)

_TIMEOUT_SECONDS = 60.0
_MIN_REQUEST_INTERVAL_SECONDS = 0.15
_TOKEN_RENEW_MARGIN_SECONDS = 60
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404

_SPORTS_BY_TYPE_ID = {
    1: "Swim",
    2: "Bike",
    3: "Run",
    4: "Brick",
    5: "Crosstrain",
    6: "Race",
    7: "DayOff",
    8: "MtnBike",
    9: "Strength",
    10: "Custom",
    11: "XCSki",
    12: "Rowing",
    13: "Walk",
    29: "Strength",
    100: "Other",
}
_ZONE_SPORTS_BY_TYPE_ID = {0: "Default", 1: "Swim", 2: "Bike", 3: "Run"}
_PACE_PER_KM_SPORTS = frozenset({"Run", "Walk"})
_EQUIPMENT_KINDS: dict[int, EquipmentKind] = {1: "bike", 2: "shoe"}
_AVAILABILITY_LIMITED = 2

_DURATION_PR_SUFFIXES = ("5sec", "1min", "5min", "10min", "20min", "60min", "90min")
_POWER_PR_TYPES = tuple(f"power{suffix}" for suffix in _DURATION_PR_SUFFIXES)
_HEART_RATE_PR_TYPES = tuple(f"hR{suffix}" for suffix in _DURATION_PR_SUFFIXES)
_SPEED_PR_DISTANCES_M = {
    "speed400Meter": 400.0,
    "speed800Meter": 800.0,
    "speed1K": 1_000.0,
    "speed1Mi": 1_609.344,
    "speed5K": 5_000.0,
    "speed5Mi": 8_046.72,
    "speed10K": 10_000.0,
    "speed10Mi": 16_093.44,
    "speedHalfMarathon": 21_097.5,
    "speedMarathon": 42_195.0,
    "speed50K": 50_000.0,
}
_PERSONAL_RECORD_TYPES: dict[PersonalRecordSport, tuple[str, ...]] = {
    "Bike": _POWER_PR_TYPES + _HEART_RATE_PR_TYPES,
    "Run": _HEART_RATE_PR_TYPES + tuple(_SPEED_PR_DISTANCES_M),
}

_WORKOUT = "Treino"
_WORKOUT_ANALYSIS = "Análise do treino"
_STRENGTH_WORKOUT = "Treino de força"
_NOTE = "Nota"
_LIBRARY = "Biblioteca"
_LIBRARY_WORKOUT = "Treino da biblioteca"

_MILES_TO_KM = 1.609344
_METERS_HEURISTIC_THRESHOLD = 1000


class TrainingPeaksError(Exception):
    """Erro esperado da API, com mensagem pensada para o LLM."""


class TrainingPeaksAuthError(TrainingPeaksError):
    def __str__(self) -> str:
        return (
            "O cookie do TrainingPeaks está ausente, expirou ou é inválido. Faça login em "
            "app.trainingpeaks.com, copie o cookie Production_tpAuth (DevTools → "
            "Application → Cookies) e atualize TRAININGPEAKS_AUTH_COOKIE: rodando local, "
            "no .env da raiz do repo (sem reiniciar); em container, na variável de ambiente "
            "do container, recriando-o em seguida."
        )


class TrainingPeaksForbiddenError(TrainingPeaksError):
    def __init__(self, path: str, /) -> None:
        self.path = path

    def __str__(self) -> str:
        return (
            f"Acesso negado a {self.path}. O recurso pode exigir conta Premium "
            "ou não pertencer a este atleta."
        )


class TrainingPeaksNotFoundError(TrainingPeaksError):
    def __init__(self, resource: str, resource_id: str | int, /) -> None:
        self.resource = resource
        self.resource_id = resource_id

    def __str__(self) -> str:
        return f"{self.resource} {self.resource_id} não encontrado no TrainingPeaks."


def personal_record_types(sport: PersonalRecordSport, /) -> tuple[str, ...]:
    return _PERSONAL_RECORD_TYPES[sport]


class _Token(BaseModel):
    value: str
    expires_at: float


class TrainingPeaksClient:  # ruff: ignore[too-many-public-methods] - um método por endpoint usado
    """
    Cliente somente leitura da API (não oficial) do TrainingPeaks.

    Autentica trocando o cookie de sessão do site por um bearer token, que fica
    em memória até expirar.
    """

    def __init__(
        self,
        *,
        auth_cookie: Callable[[], str],
        transport: AsyncBaseTransport | None = None,
        min_request_interval_seconds: float = _MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._auth_cookie = auth_cookie
        self._min_request_interval_seconds = min_request_interval_seconds
        self._api = _build_client(_API_URL, transport)
        self._peaksware = _build_client(
            _PEAKSWARE_URL,
            transport,
            headers={"Origin": _APP_ORIGIN, "Referer": f"{_APP_ORIGIN}/"},
        )
        self._token: _Token | None = None
        self._token_lock = asyncio.Lock()
        self._athlete_id: int | None = None
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._peaksware.aclose()

    # ------------------------------------------------------------ conta

    async def get_account(self) -> AccountModel:
        payload = await self._get(self._api, "/users/v3/user")
        raw = _RawUserResponse.model_validate(payload).user
        self._athlete_id = raw.person_id
        return _to_account_model(raw)

    async def get_athlete_settings(self) -> AthleteSettingsModel:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(self._api, f"/fitness/v1/athletes/{athlete_id}/settings")
        return _to_athlete_settings_model(_RawSettings.model_validate(payload))

    async def list_pool_lengths(self) -> list[PoolLengthModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v1/athletes/{athlete_id}/poollengthsettings",
        )
        raw = _RawPoolLengths.model_validate(payload or {})
        return [
            PoolLengthModel(
                length=option.length,
                unit=option.units,
                is_default=option.id == raw.default_id,
            )
            for option in raw.options
        ]

    # ------------------------------------------------------------ treinos

    async def list_workouts(self, start: date, end: date) -> list[WorkoutSummaryModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v6/athletes/{athlete_id}/workouts/{start}/{end}",
        )
        raw_workouts = _validate_list(_RawWorkout, payload)
        return [_to_workout_summary_model(raw) for raw in raw_workouts]

    async def get_workout(self, workout_id: int) -> WorkoutDetailModel:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v6/athletes/{athlete_id}/workouts/{workout_id}",
            not_found=(_WORKOUT, workout_id),
        )
        if not payload:
            raise TrainingPeaksNotFoundError(_WORKOUT, workout_id)
        return _to_workout_detail_model(_RawWorkout.model_validate(payload))

    async def list_workout_files(self, workout_id: int) -> list[WorkoutFileModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v6/athletes/{athlete_id}/workouts/{workout_id}/details",
            not_found=(_WORKOUT, workout_id),
        )
        raw = _RawWorkoutDetails.model_validate(payload or {})
        return [
            WorkoutFileModel(name=file.file_name, uploaded_at=file.date_uploaded)
            for file in raw.device_files + raw.attachment_files
        ]

    async def get_private_note(self, workout_id: int) -> str | None:
        payload = await self._get(
            self._api,
            f"/fitness/v6/workouts/{workout_id}/privateWorkoutNote",
            not_found=(_WORKOUT, workout_id),
        )
        return _RawPrivateNote.model_validate(payload or {}).note or None

    async def list_workout_personal_records(
        self,
        workout_id: int,
    ) -> list[WorkoutPersonalRecordModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/personalrecord/v2/athletes/{athlete_id}/workouts/{workout_id}",
            params={"displayPeaksForBasic": "true"},
            not_found=(_WORKOUT, workout_id),
        )
        raw = _RawWorkoutPersonalRecords.model_validate(payload or {})
        return [
            WorkoutPersonalRecordModel(
                kind=record.record_class,
                type=record.type,
                timeframe=record.time_frame.name if record.time_frame else None,
                rank=record.rank,
                value=record.value,
            )
            for record in raw.personal_records
        ]

    async def get_workout_analysis(self, workout_id: int) -> WorkoutAnalysisModel:
        body: dict[str, JsonValue] = {"workoutId": workout_id}
        summary = await self._post(
            self._peaksware,
            "/workout-analysis/v2/analyze/summary",
            body,
            not_found=(_WORKOUT_ANALYSIS, workout_id),
        )
        charts = await self._post_optional(
            self._peaksware,
            "/workout-analysis/v2/analyze/charts",
            body,
        )
        laps = await self._post_optional(
            self._peaksware,
            "/workout-analysis/v2/analyze/laps",
            body,
        )
        return _to_workout_analysis_model(
            workout_id,
            _RawAnalysisSummary.model_validate(summary or {}),
            _RawAnalysisCharts.model_validate(charts or {}),
            _RawAnalysisLaps.model_validate(laps or {}),
        )

    # ------------------------------------------------------------ performance

    async def list_personal_records(
        self,
        sport: PersonalRecordSport,
        pr_type: str,
        start: date,
        end: date,
    ) -> list[PersonalRecordModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/personalrecord/v2/athletes/{athlete_id}/{sport}",
            params={
                "prType": pr_type,
                "startDate": f"{start}T00:00:00",
                "endDate": f"{end}T00:00:00",
            },
        )
        raw_records = _validate_list(_RawPersonalRecord, payload)
        return [_to_personal_record_model(pr_type, raw) for raw in raw_records]

    async def list_fitness(self, start: date, end: date) -> list[FitnessDayModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._post(
            self._api,
            f"/fitness/v1/athletes/{athlete_id}/reporting/performancedata/{start}/{end}",
            {
                "atlConstant": 7,
                "atlStart": 0,
                "ctlConstant": 42,
                "ctlStart": 0,
                "workoutTypes": [],
            },
        )
        return [
            FitnessDayModel(
                date=_to_date(raw.workout_day) or start,
                tss=round(raw.tss_actual or 0, 1),
                ctl=round(raw.ctl or 0, 1),
                atl=round(raw.atl or 0, 1),
                tsb=round(raw.tsb or 0, 1),
            )
            for raw in _validate_list(_RawFitnessDay, payload)
        ]

    async def list_atp_weeks(self, start: date, end: date) -> list[AtpWeekModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v1/athletes/{athlete_id}/atp/{start}/{end}",
        )
        return [
            AtpWeekModel(
                week_start=_to_date(raw.week),
                planned_tss=raw.volume,
                period=raw.period or None,
                race_name=raw.race_name or None,
                race_priority=raw.race_priority or None,
                weeks_to_priority_event=raw.weeks_to_next_priority_event,
            )
            for raw in _validate_list(_RawAtpWeek, payload)
        ]

    # ------------------------------------------------------------ calendário

    async def get_focus_event(self) -> EventModel | None:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v6/athletes/{athlete_id}/events/focusevent",
        )
        return _to_event_model(_RawEvent.model_validate(payload)) if payload else None

    async def get_next_event(self) -> EventModel | None:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v6/athletes/{athlete_id}/events/nextplannedevent",
        )
        return _to_event_model(_RawEvent.model_validate(payload)) if payload else None

    async def list_events(self, start: date, end: date) -> list[EventModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v6/athletes/{athlete_id}/events/{start}/{end}",
        )
        return [_to_event_model(raw) for raw in _validate_list(_RawEvent, payload)]

    async def list_calendar_notes(self, start: date, end: date) -> list[CalendarNoteModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v2/athletes/{athlete_id}/calendarNote/{start}/{end}",
        )
        return [
            CalendarNoteModel(
                id=raw.id,
                date=_to_date(raw.note_date),
                title=raw.title,
                description=raw.description,
                is_hidden=raw.is_hidden,
                comment_count=raw.comment_count,
            )
            for raw in _validate_list(_RawCalendarNote, payload)
        ]

    async def list_note_comments(self, note_id: int) -> list[NoteCommentModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v1/athletes/{athlete_id}/calendarNote/{note_id}/comments",
            not_found=(_NOTE, note_id),
        )
        return [
            NoteCommentModel(
                author=_full_name(raw.first_name, raw.last_name),
                created_at=raw.created_date_time_utc,
                comment=raw.comment or "",
            )
            for raw in _validate_list(_RawNoteComment, payload)
        ]

    async def list_availability(self, start: date, end: date) -> list[AvailabilityModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v1/athletes/{athlete_id}/availability/{start}/{end}",
        )
        return [
            AvailabilityModel(
                id=raw.id,
                start_date=_to_date(raw.start_date),
                end_date=_to_date(raw.end_date),
                kind="limited"
                if raw.limited_availability or raw.type == _AVAILABILITY_LIMITED
                else "unavailable",
                available_sports=[
                    _SPORTS_BY_TYPE_ID.get(sport_id, str(sport_id))
                    for sport_id in raw.available_sport_types
                ],
                reason=raw.reason or None,
                description=raw.description or None,
            )
            for raw in _validate_list(_RawAvailability, payload)
        ]

    # ------------------------------------------------------------ saúde

    async def list_health_metrics(
        self,
        start: date,
        end: date,
    ) -> list[HealthMetricEntryModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/metrics/v3/athletes/{athlete_id}/consolidatedtimedmetrics/{start}/{end}",
        )
        return [
            HealthMetricEntryModel(
                timestamp=raw.time_stamp,
                values=[
                    MetricValueModel(label=detail.label or str(detail.type), value=detail.value)
                    for detail in raw.details
                ],
            )
            for raw in _validate_list(_RawMetricEntry, payload)
        ]

    async def list_nutrition(self, start: date, end: date) -> list[JsonValue]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._api,
            f"/fitness/v1/athletes/{athlete_id}/nutrition/{start}/{end}",
        )
        entries = payload if isinstance(payload, list) else [payload] if payload else []
        return [compact_json(entry) for entry in entries]

    async def list_equipment(self) -> list[EquipmentModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(self._api, f"/fitness/v1/athletes/{athlete_id}/equipment")
        return [
            EquipmentModel(
                id=raw.equipment_id,
                name=raw.name,
                kind=_EQUIPMENT_KINDS.get(raw.equipment_type or 0, "other"),
                brand=raw.brand or None,
                model=raw.model or None,
                distance_km=meters_to_km(raw.actual_distance),
                starting_distance_km=meters_to_km(raw.starting_distance),
                max_distance_km=meters_to_km(raw.max_distance) if raw.max_distance else None,
                retired=raw.retired,
                retired_date=_to_date(raw.retired_date),
                is_default=raw.is_default,
                purchase_date=_to_date(raw.date_of_purchase),
                notes=raw.notes or None,
            )
            for raw in _validate_list(_RawEquipment, payload)
        ]

    # ------------------------------------------------------------ força

    async def list_strength_workouts(
        self,
        start: date,
        end: date,
    ) -> list[StrengthWorkoutSummaryModel]:
        athlete_id = await self._get_athlete_id()
        payload = await self._get(
            self._peaksware,
            f"/rx/activity/v1/workouts/calendar/{athlete_id}/{start}/{end}",
        )
        items = payload.get("data") if isinstance(payload, dict) else payload
        return [
            StrengthWorkoutSummaryModel(
                id=str(raw.id),
                date=_to_date(raw.prescribed_date),
                title=raw.title,
                workout_type=raw.workout_type,
                prescribed_duration_min=_seconds_to_minutes(raw.prescribed_duration_in_seconds),
                compliance_state=raw.compliance_state,
                compliance_percent=round_or_none(raw.compliance_percent),
                total_sets=raw.total_sets,
                completed_sets=raw.completed_sets,
                exercises=[step.title for step in raw.sequence_summary if step.title],
            )
            for raw in _validate_list(_RawStrengthSummary, items)
        ]

    async def get_strength_workout(self, workout_id: str) -> StrengthWorkoutModel:
        payload = await self._get(
            self._peaksware,
            f"/rx/activity/v1/workouts/{workout_id}",
            not_found=(_STRENGTH_WORKOUT, workout_id),
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            raise TrainingPeaksNotFoundError(_STRENGTH_WORKOUT, workout_id)
        return _to_strength_workout_model(_RawStrengthWorkout.model_validate(data))

    # ------------------------------------------------------------ biblioteca

    async def list_workout_libraries(self) -> list[WorkoutLibraryModel]:
        payload = await self._get(self._api, "/exerciselibrary/v2/libraries")
        return [
            WorkoutLibraryModel(
                id=raw.exercise_library_id,
                name=raw.library_name,
                owner_name=raw.owner_name,
                is_default=raw.is_default_content,
            )
            for raw in _validate_list(_RawLibrary, payload)
        ]

    async def list_library_workouts(self, library_id: int) -> list[LibraryWorkoutModel]:
        payload = await self._get(
            self._api,
            f"/exerciselibrary/v2/libraries/{library_id}/items",
            not_found=(_LIBRARY, library_id),
        )
        return [
            _to_library_workout_model(library_id, raw)
            for raw in _validate_list(_RawLibraryItem, payload)
        ]

    async def get_library_workout(self, library_id: int, workout_id: int) -> LibraryWorkoutModel:
        # A API não tem endpoint por item: busca a biblioteca inteira e filtra
        for workout in await self.list_library_workouts(library_id):
            if workout.id == workout_id:
                return workout
        raise TrainingPeaksNotFoundError(_LIBRARY_WORKOUT, workout_id)

    # ------------------------------------------------------------ HTTP

    async def _get_athlete_id(self) -> int:
        if self._athlete_id is None:
            await self.get_account()
        if self._athlete_id is None:
            raise TrainingPeaksAuthError
        return self._athlete_id

    async def _get(
        self,
        http: AsyncClient,
        path: str,
        *,
        params: dict[str, str] | None = None,
        not_found: tuple[str, str | int] | None = None,
    ) -> JsonValue:
        return await self._request(http, "GET", path, params=params, not_found=not_found)

    async def _post(
        self,
        http: AsyncClient,
        path: str,
        body: dict[str, JsonValue],
        *,
        not_found: tuple[str, str | int] | None = None,
    ) -> JsonValue:
        return await self._request(http, "POST", path, body=body, not_found=not_found)

    async def _post_optional(
        self,
        http: AsyncClient,
        path: str,
        body: dict[str, JsonValue],
    ) -> JsonValue:
        # Treino sem arquivo do dispositivo não tem gráficos/voltas: 404 vira vazio
        try:
            return await self._post(http, path, body)
        except HTTPStatusError as error:
            if error.response.status_code == _HTTP_NOT_FOUND:
                return None
            raise

    async def _request(
        self,
        http: AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, JsonValue] | None = None,
        not_found: tuple[str, str | int] | None,
    ) -> JsonValue:
        for attempt in (1, 2):
            token = await self._get_access_token()
            try:
                response = await self._send(
                    http,
                    method,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    body=body,
                )
            except HTTPStatusError as error:
                status = error.response.status_code
                if status == _HTTP_UNAUTHORIZED and attempt == 1:
                    # Token pode ter expirado no meio do caminho: renova uma vez
                    self._token = None
                    continue
                if status == _HTTP_UNAUTHORIZED:
                    raise TrainingPeaksAuthError from error
                if status == _HTTP_FORBIDDEN:
                    raise TrainingPeaksForbiddenError(path) from error
                if status == _HTTP_NOT_FOUND and not_found is not None:
                    raise TrainingPeaksNotFoundError(*not_found) from error
                raise
            return _parse_json(response)
        raise TrainingPeaksAuthError

    async def _get_access_token(self) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if self._token is None or self._token.expires_at - _TOKEN_RENEW_MARGIN_SECONDS <= now:
                self._token = await self._exchange_cookie_for_token()
            return self._token.value

    async def _exchange_cookie_for_token(self) -> _Token:
        cookie = self._auth_cookie().strip()
        if not cookie:
            raise TrainingPeaksAuthError
        try:
            response = await self._send(
                self._api,
                "GET",
                "/users/v3/token",
                headers={"Cookie": f"{_AUTH_COOKIE_NAME}={cookie}"},
            )
        except HTTPStatusError as error:
            if error.response.status_code in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
                raise TrainingPeaksAuthError from error
            raise
        raw = _RawTokenResponse.model_validate(_parse_json(response))
        if not raw.success or raw.token is None:
            raise TrainingPeaksAuthError
        logger.info("Token do TrainingPeaks renovado")
        return _Token(
            value=raw.token.access_token,
            expires_at=time.monotonic() + raw.token.expires_in,
        )

    @retry(max_attempts=3, when=is_retryable_http_error)
    async def _send(
        self,
        http: AsyncClient,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        body: dict[str, JsonValue] | None = None,
    ) -> Response:
        await self._throttle()
        return await http.request(method, path, headers=headers, params=params, json=body)

    async def _throttle(self) -> None:
        # Espaça as requisições para não tomar rate limit
        async with self._throttle_lock:
            wait = self._last_request_at + self._min_request_interval_seconds - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


def _build_client(
    base_url: str,
    transport: AsyncBaseTransport | None,
    *,
    headers: dict[str, str] | None = None,
) -> AsyncClient:
    return AsyncClient(
        base_url=base_url,
        timeout=_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", **(headers or {})},
        transport=ReadOnlyTransport(
            AutoRaiseTransport(transport),
            read_only_post_paths=_READ_ONLY_POST_PATHS,
        ),
    )


def _parse_json(response: Response) -> JsonValue:
    if not response.content:
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def _validate_list[T: BaseModel](model: type[T], payload: JsonValue) -> list[T]:
    if not isinstance(payload, list):
        return []
    return [model.model_validate(item) for item in payload if isinstance(item, dict)]


# ---------------------------------------------------------------- respostas cruas


class _RawModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: object) -> object:
        # A API manda `null` onde deveria mandar lista/objeto vazio (ex.: `attachmentFileInfos`
        # de treino planejado); sem a chave, vale o default do campo
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class _RawTokenData(_RawModel):
    access_token: str
    expires_in: float = 3600


class _RawTokenResponse(_RawModel):
    success: bool = False
    token: _RawTokenData | None = None


class _RawAccountSettings(_RawModel):
    is_premium: bool = Field(default=False, alias="isPremium")


class _RawUserSettings(_RawModel):
    account: _RawAccountSettings = Field(default_factory=_RawAccountSettings)


class _RawUser(_RawModel):
    person_id: int = Field(alias="personId")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    full_name: str | None = Field(default=None, alias="fullName")
    email: str | None = None
    settings: _RawUserSettings = Field(default_factory=_RawUserSettings)


class _RawUserResponse(_RawModel):
    user: _RawUser


class _RawZone(_RawModel):
    label: str | None = None
    minimum: float = 0
    maximum: float = 0


class _RawZoneSet(_RawModel):
    workout_type_id: int = Field(default=0, alias="workoutTypeId")
    threshold: float | None = None
    calculation_method: int | None = Field(default=None, alias="calculationMethod")
    maximum_heart_rate: int | None = Field(default=None, alias="maximumHeartRate")
    resting_heart_rate: int | None = Field(default=None, alias="restingHeartRate")
    zones: list[_RawZone] = Field(default_factory=list)


class _RawNutritionSettings(_RawModel):
    planned_calories: int | None = Field(default=None, alias="plannedCalories")


class _RawSettings(_RawModel):
    full_name: str | None = Field(default=None, alias="fullName")
    email: str | None = None
    gender: str | None = None
    birthday: str | None = None
    age: int | None = None
    city: str | None = None
    time_zone: str | None = Field(default=None, alias="timeZone")
    expire_date: str | None = Field(default=None, alias="expireDate")
    nutrition_settings: _RawNutritionSettings | None = Field(
        default=None,
        alias="nutritionSettings",
    )
    power_zones: list[_RawZoneSet] = Field(default_factory=list, alias="powerZones")
    heart_rate_zones: list[_RawZoneSet] = Field(default_factory=list, alias="heartRateZones")
    speed_zones: list[_RawZoneSet] = Field(default_factory=list, alias="speedZones")


class _RawPoolLengthOption(_RawModel):
    id: str
    length: float
    units: str


class _RawPoolLengths(_RawModel):
    default_id: str | None = Field(default=None, alias="defaultId")
    options: list[_RawPoolLengthOption] = Field(default_factory=list)


class _RawWorkoutComment(_RawModel):
    comment: str | None = None
    commenter_name: str | None = Field(default=None, alias="commenterName")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    is_coach: bool = Field(default=False, alias="isCoach")
    date_created: datetime | None = Field(default=None, alias="dateCreated")


class _RawWorkout(_RawModel):
    workout_id: int = Field(alias="workoutId")
    workout_day: str = Field(alias="workoutDay")
    start_time: datetime | None = Field(default=None, alias="startTime")
    title: str | None = None
    workout_type_value_id: int | None = Field(default=None, alias="workoutTypeValueId")
    completed: bool | None = None
    total_time_planned: float | None = Field(default=None, alias="totalTimePlanned")
    total_time: float | None = Field(default=None, alias="totalTime")
    distance_planned: float | None = Field(default=None, alias="distancePlanned")
    distance: float | None = None
    tss_planned: float | None = Field(default=None, alias="tssPlanned")
    tss_actual: float | None = Field(default=None, alias="tssActual")
    if_planned: float | None = Field(default=None, alias="ifPlanned")
    if_actual: float | None = Field(default=None, alias="if")
    heart_rate_average: float | None = Field(default=None, alias="heartRateAverage")
    heart_rate_maximum: float | None = Field(default=None, alias="heartRateMaximum")
    power_average: float | None = Field(default=None, alias="powerAverage")
    power_maximum: float | None = Field(default=None, alias="powerMaximum")
    normalized_power_actual: float | None = Field(default=None, alias="normalizedPowerActual")
    cadence_average: float | None = Field(default=None, alias="cadenceAverage")
    velocity_average: float | None = Field(default=None, alias="velocityAverage")
    velocity_maximum: float | None = Field(default=None, alias="velocityMaximum")
    elevation_gain: float | None = Field(default=None, alias="elevationGain")
    elevation_loss: float | None = Field(default=None, alias="elevationLoss")
    calories: float | None = None
    rpe: int | None = None
    feeling: int | None = None
    description: str | None = None
    coach_comments: str | None = Field(default=None, alias="coachComments")
    structure: JsonValue = None
    workout_comments: list[_RawWorkoutComment] = Field(
        default_factory=list,
        alias="workoutComments",
    )
    equipment_shoe_id: int | None = Field(default=None, alias="equipmentShoeId")
    equipment_bike_id: int | None = Field(default=None, alias="equipmentBikeId")
    has_private_note: bool = Field(default=False, alias="hasPrivateWorkoutNoteForCaller")


class _RawWorkoutFile(_RawModel):
    file_name: str | None = Field(default=None, alias="fileName")
    date_uploaded: datetime | None = Field(default=None, alias="dateUploaded")


class _RawWorkoutDetails(_RawModel):
    device_files: list[_RawWorkoutFile] = Field(
        default_factory=list,
        alias="workoutDeviceFileInfos",
    )
    attachment_files: list[_RawWorkoutFile] = Field(
        default_factory=list,
        alias="attachmentFileInfos",
    )


class _RawPrivateNote(_RawModel):
    note: str | None = None


class _RawTimeFrame(_RawModel):
    name: str | None = None


class _RawWorkoutPersonalRecord(_RawModel):
    record_class: str | None = Field(default=None, alias="class")
    type: str | None = None
    value: float | None = None
    rank: int | None = None
    time_frame: _RawTimeFrame | None = Field(default=None, alias="timeFrame")


class _RawWorkoutPersonalRecords(_RawModel):
    personal_records: list[_RawWorkoutPersonalRecord] = Field(
        default_factory=list,
        alias="personalRecords",
    )


class _RawAnalysisValue(_RawModel):
    friendly_name: str | None = Field(default=None, alias="friendlyName")
    value: JsonValue = None
    unit: str | None = None


class _RawAnalysisSummary(_RawModel):
    start_timestamp: datetime | None = Field(default=None, alias="startTimestamp")
    data: dict[str, _RawAnalysisValue] = Field(default_factory=dict)


class _RawAnalysisZone(_RawModel):
    label: str | None = None
    min: float | None = None
    max: float | None = None


class _RawAnalysisChannel(_RawModel):
    friendly_name: str | None = Field(default=None, alias="friendlyName")
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    average: float | None = None
    zones: list[_RawAnalysisZone] | None = None


class _RawAnalysisCharts(_RawModel):
    metadata: dict[str, _RawAnalysisChannel] = Field(default_factory=dict)
    data: list[dict[str, JsonValue]] = Field(default_factory=list)


class _RawLapColumn(_RawModel):
    friendly_name: str | None = Field(default=None, alias="friendlyName")
    unit: str | None = None


class _RawAnalysisLaps(_RawModel):
    column_metadata: dict[str, _RawLapColumn] = Field(
        default_factory=dict,
        alias="columnMetadata",
    )
    data: list[dict[str, JsonValue]] = Field(default_factory=list)


class _RawPersonalRecord(_RawModel):
    rank: int | None = None
    value: float | None = None
    workout_id: int | None = Field(default=None, alias="workoutId")
    workout_title: str | None = Field(default=None, alias="workoutTitle")
    workout_date: str | None = Field(default=None, alias="workoutDate")


class _RawFitnessDay(_RawModel):
    workout_day: str | None = Field(default=None, alias="workoutDay")
    tss_actual: float | None = Field(default=None, alias="tssActual")
    ctl: float | None = None
    atl: float | None = None
    tsb: float | None = None


class _RawAtpWeek(_RawModel):
    week: str | None = None
    volume: float | None = None
    period: str | None = None
    race_name: str | None = Field(default=None, alias="raceName")
    race_priority: str | None = Field(default=None, alias="racePriority")
    weeks_to_next_priority_event: int | None = Field(
        default=None,
        alias="weeksToNextPriorityEvent",
    )


class _RawEvent(_RawModel):
    id: int
    name: str | None = None
    event_date: str | None = Field(default=None, alias="eventDate")
    event_type: str | None = Field(default=None, alias="eventType")
    atp_priority: str | None = Field(default=None, alias="atpPriority")
    distance: float | None = None
    distance_units: str | None = Field(default=None, alias="distanceUnits")
    ctl_target: float | None = Field(default=None, alias="ctlTarget")
    description: str | None = None
    comment: str | None = None
    goals: JsonValue = None
    results: JsonValue = None


class _RawCalendarNote(_RawModel):
    id: int = Field(validation_alias=AliasChoices("id", "calendarNoteId"))
    title: str | None = None
    description: str | None = None
    note_date: str | None = Field(default=None, alias="noteDate")
    is_hidden: bool = Field(default=False, alias="isHidden")
    comment_count: int = Field(default=0, alias="commentCount")


class _RawNoteComment(_RawModel):
    comment: str | None = None
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    created_date_time_utc: datetime | None = Field(default=None, alias="createdDateTimeUtc")


class _RawAvailability(_RawModel):
    id: int
    start_date: str | None = Field(default=None, alias="startDate")
    end_date: str | None = Field(default=None, alias="endDate")
    type: int | None = None
    limited_availability: bool = Field(default=False, alias="limitedAvailability")
    available_sport_types: list[int] = Field(default_factory=list, alias="availableSportTypes")
    reason: str | None = None
    description: str | None = None


class _RawMetricDetail(_RawModel):
    label: str | None = None
    type: int | None = None
    value: JsonValue = None


class _RawMetricEntry(_RawModel):
    time_stamp: datetime | None = Field(default=None, alias="timeStamp")
    details: list[_RawMetricDetail] = Field(default_factory=list)


class _RawEquipment(_RawModel):
    equipment_id: int = Field(alias="equipmentId")
    name: str | None = None
    equipment_type: int | None = Field(
        default=None,
        validation_alias=AliasChoices("type", "equipmentType"),
    )
    brand: str | None = None
    model: str | None = None
    # Total do equipamento, já somando `startingDistance`
    actual_distance: float | None = Field(
        default=None,
        validation_alias=AliasChoices("actualDistance", "distance"),
    )
    starting_distance: float | None = Field(default=None, alias="startingDistance")
    max_distance: float | None = Field(default=None, alias="maxDistance")
    retired: bool = False
    retired_date: str | None = Field(default=None, alias="retiredDate")
    is_default: bool = Field(default=False, alias="isDefault")
    date_of_purchase: str | None = Field(default=None, alias="dateOfPurchase")
    notes: str | None = None


class _RawSequenceStep(_RawModel):
    title: str | None = None


class _RawStrengthSummary(_RawModel):
    id: int | str
    prescribed_date: str | None = Field(default=None, alias="prescribedDate")
    title: str | None = None
    workout_type: str | None = Field(default=None, alias="workoutType")
    prescribed_duration_in_seconds: float | None = Field(
        default=None,
        alias="prescribedDurationInSeconds",
    )
    compliance_state: str | None = Field(default=None, alias="complianceState")
    compliance_percent: float | None = Field(default=None, alias="compliancePercent")
    total_sets: int | None = Field(default=None, alias="totalSets")
    completed_sets: int | None = Field(default=None, alias="completedSets")
    sequence_summary: list[_RawSequenceStep] = Field(
        default_factory=list,
        alias="sequenceSummary",
    )


class _RawStrengthParameterValue(_RawModel):
    parameter: str | None = None
    prescribed_value: JsonValue = Field(default=None, alias="prescribedValue")
    executed_value: JsonValue = Field(default=None, alias="executedValue")


class _RawStrengthSet(_RawModel):
    is_complete: bool = Field(default=False, alias="isComplete")
    parameter_values: list[_RawStrengthParameterValue] = Field(
        default_factory=list,
        alias="parameterValues",
    )


class _RawStrengthExercise(_RawModel):
    title: str | None = None


class _RawStrengthPrescription(_RawModel):
    exercise: _RawStrengthExercise | None = None
    coach_notes: str | None = Field(default=None, alias="coachNotes")
    compliance_percent: float | None = Field(default=None, alias="compliancePercent")
    sets: list[_RawStrengthSet] = Field(default_factory=list)


class _RawStrengthBlock(_RawModel):
    block_type: str | None = Field(default=None, alias="blockType")
    title: str | None = None
    coach_notes: str | None = Field(default=None, alias="coachNotes")
    compliance_percent: float | None = Field(default=None, alias="compliancePercent")
    prescriptions: list[_RawStrengthPrescription] = Field(default_factory=list)


class _RawStrengthSnapshot(_RawModel):
    total_sets: int | None = Field(default=None, alias="totalSets")
    completed_sets: int | None = Field(default=None, alias="completedSets")


class _RawStrengthWorkout(_RawModel):
    id: int | str
    prescribed_date: str | None = Field(default=None, alias="prescribedDate")
    title: str | None = None
    workout_type: str | None = Field(default=None, alias="workoutType")
    instructions: str | None = None
    prescribed_duration_in_seconds: float | None = Field(
        default=None,
        alias="prescribedDurationInSeconds",
    )
    executed_duration_in_seconds: float | None = Field(
        default=None,
        alias="executedDurationInSeconds",
    )
    compliance_state: str | None = Field(default=None, alias="complianceState")
    compliance_percent: float | None = Field(default=None, alias="compliancePercent")
    rpe: int | None = None
    feel: int | None = None
    completed_tss: float | None = Field(default=None, alias="completedTss")
    snapshot: _RawStrengthSnapshot = Field(default_factory=_RawStrengthSnapshot)
    blocks: list[_RawStrengthBlock] = Field(default_factory=list)


class _RawLibrary(_RawModel):
    exercise_library_id: int = Field(alias="exerciseLibraryId")
    library_name: str | None = Field(default=None, alias="libraryName")
    owner_name: str | None = Field(default=None, alias="ownerName")
    is_default_content: bool = Field(default=False, alias="isDefaultContent")


class _RawLibraryItem(_RawModel):
    exercise_library_item_id: int = Field(alias="exerciseLibraryItemId")
    item_name: str | None = Field(default=None, alias="itemName")
    workout_type_id: int | None = Field(default=None, alias="workoutTypeId")
    total_time_planned: float | None = Field(default=None, alias="totalTimePlanned")
    distance_planned: float | None = Field(default=None, alias="distancePlanned")
    tss_planned: float | None = Field(default=None, alias="tssPlanned")
    if_planned: float | None = Field(default=None, alias="ifPlanned")
    description: str | None = None
    coach_comments: str | None = Field(default=None, alias="coachComments")
    structure: JsonValue = None


# ---------------------------------------------------------------- conversores


def _to_account_model(raw: _RawUser) -> AccountModel:
    return AccountModel(
        athlete_id=raw.person_id,
        name=raw.full_name or _full_name(raw.first_name, raw.last_name) or "",
        email=raw.email,
        is_premium=raw.settings.account.is_premium,
    )


def _to_athlete_settings_model(raw: _RawSettings) -> AthleteSettingsModel:
    return AthleteSettingsModel(
        full_name=raw.full_name,
        email=raw.email,
        gender=raw.gender,
        birthday=_to_date(raw.birthday),
        age=raw.age,
        city=raw.city,
        timezone=raw.time_zone,
        account_expires_on=_to_date(raw.expire_date),
        planned_daily_calories=raw.nutrition_settings.planned_calories
        if raw.nutrition_settings
        else None,
        power_zones=[_to_zone_set_model(zone_set, unit="W") for zone_set in raw.power_zones],
        heart_rate_zones=[
            _to_zone_set_model(zone_set, unit="bpm") for zone_set in raw.heart_rate_zones
        ],
        speed_zones=[_to_speed_zone_set_model(zone_set) for zone_set in raw.speed_zones],
    )


def _to_zone_set_model(raw: _RawZoneSet, *, unit: str) -> ZoneSetModel:
    return ZoneSetModel(
        sport=_ZONE_SPORTS_BY_TYPE_ID.get(raw.workout_type_id, str(raw.workout_type_id)),
        unit=unit,
        threshold=raw.threshold,
        maximum_heart_rate=raw.maximum_heart_rate,
        resting_heart_rate=raw.resting_heart_rate,
        calculation_method=raw.calculation_method,
        zones=[
            ZoneModel(label=zone.label, minimum=zone.minimum, maximum=zone.maximum)
            for zone in raw.zones
        ],
    )


def _to_speed_zone_set_model(raw: _RawZoneSet) -> ZoneSetModel:
    sport = _ZONE_SPORTS_BY_TYPE_ID.get(raw.workout_type_id, str(raw.workout_type_id))
    per_meters = 100 if sport == "Swim" else 1000
    return ZoneSetModel(
        sport=sport,
        unit="m/s",
        threshold=round_or_none(raw.threshold, 3),
        threshold_pace=format_pace(raw.threshold, per_meters=per_meters),
        calculation_method=raw.calculation_method,
        zones=[
            ZoneModel(
                label=zone.label,
                minimum=round(zone.minimum, 3),
                maximum=round(zone.maximum, 3),
                # Pace é o inverso da velocidade: o mínimo de velocidade é o pace mais lento
                minimum_pace=format_pace(zone.minimum, per_meters=per_meters),
                maximum_pace=format_pace(zone.maximum, per_meters=per_meters),
            )
            for zone in raw.zones
        ],
    )


def _workout_summary_fields(raw: _RawWorkout) -> dict[str, object]:
    sport = _SPORTS_BY_TYPE_ID.get(raw.workout_type_value_id or 0)
    completed = bool(raw.completed) or bool(raw.total_time)
    return {
        "id": raw.workout_id,
        "date": _to_date(raw.workout_day),
        "start_time": raw.start_time,
        "title": raw.title,
        "sport": sport,
        "completed": completed,
        "duration_planned_min": hours_to_minutes(raw.total_time_planned),
        "duration_min": hours_to_minutes(raw.total_time),
        "distance_planned_km": meters_to_km(raw.distance_planned),
        "distance_km": meters_to_km(raw.distance),
        "tss_planned": round_or_none(raw.tss_planned),
        "tss": round_or_none(raw.tss_actual),
        "intensity_factor": round_or_none(raw.if_actual, 2),
        "average_heart_rate_bpm": _round_int(raw.heart_rate_average),
        "average_power_w": round_or_none(raw.power_average, 0),
        "normalized_power_w": round_or_none(raw.normalized_power_actual, 0),
        "average_pace_per_km": format_pace(raw.velocity_average)
        if sport in _PACE_PER_KM_SPORTS
        else None,
        "average_pace_per_100m": format_pace(raw.velocity_average, per_meters=100)
        if sport == "Swim"
        else None,
        "average_speed_kmh": mps_to_kmh(raw.velocity_average),
        "elevation_gain_m": round_or_none(raw.elevation_gain, 0),
        "calories_kcal": round_or_none(raw.calories, 0),
        "rpe": raw.rpe,
        "feeling": raw.feeling,
        "description": raw.description or None,
        "shoe_id": raw.equipment_shoe_id,
        "bike_id": raw.equipment_bike_id,
    }


def _to_workout_summary_model(raw: _RawWorkout) -> WorkoutSummaryModel:
    return WorkoutSummaryModel.model_validate(_workout_summary_fields(raw))


def _to_workout_detail_model(raw: _RawWorkout) -> WorkoutDetailModel:
    return WorkoutDetailModel.model_validate(
        {
            **_workout_summary_fields(raw),
            "intensity_factor_planned": round_or_none(raw.if_planned, 2),
            "maximum_heart_rate_bpm": _round_int(raw.heart_rate_maximum),
            "maximum_power_w": round_or_none(raw.power_maximum, 0),
            "average_cadence": round_or_none(raw.cadence_average, 0),
            "maximum_speed_kmh": mps_to_kmh(raw.velocity_maximum),
            "elevation_loss_m": round_or_none(raw.elevation_loss, 0),
            "coach_comments": raw.coach_comments or None,
            "has_private_note": raw.has_private_note,
            "structure": _decode_structure(raw.structure),
            "comments": [_to_workout_comment_model(comment) for comment in raw.workout_comments],
        },
    )


def _to_workout_comment_model(raw: _RawWorkoutComment) -> WorkoutCommentModel:
    return WorkoutCommentModel(
        author=raw.commenter_name or _full_name(raw.first_name, raw.last_name),
        is_coach=raw.is_coach,
        created_at=raw.date_created,
        comment=raw.comment or "",
    )


def _decode_structure(structure: JsonValue) -> JsonValue:
    # A estrutura às vezes vem serializada como string JSON
    if isinstance(structure, str):
        try:
            structure = json.loads(structure)
        except json.JSONDecodeError:
            return None
    return compact_json(structure) if structure else None


def _to_workout_analysis_model(
    workout_id: int,
    summary: _RawAnalysisSummary,
    charts: _RawAnalysisCharts,
    laps: _RawAnalysisLaps,
) -> WorkoutAnalysisModel:
    return WorkoutAnalysisModel(
        workout_id=workout_id,
        start_time=summary.start_timestamp,
        totals=[
            AnalysisTotalModel(
                name=total.friendly_name or identifier,
                value=total.value,
                unit=total.unit,
            )
            for identifier, total in summary.data.items()
        ],
        channels=[
            AnalysisChannelModel(
                identifier=identifier,
                name=channel.friendly_name,
                unit=channel.unit,
                minimum=round_or_none(channel.minimum, 2),
                maximum=round_or_none(channel.maximum, 2),
                average=round_or_none(channel.average, 2),
                zones=[
                    AnalysisZoneModel(label=zone.label, minimum=zone.min, maximum=zone.max)
                    for zone in channel.zones or []
                ],
            )
            for identifier, channel in charts.metadata.items()
        ],
        lap_columns=[
            AnalysisLapColumnModel(
                identifier=identifier,
                name=column.friendly_name,
                unit=column.unit,
            )
            for identifier, column in laps.column_metadata.items()
        ],
        laps=[_compact_dict(lap) for lap in laps.data],
        time_series_total_points=len(charts.data),
        time_series=charts.data,
    )


def _to_personal_record_model(pr_type: str, raw: _RawPersonalRecord) -> PersonalRecordModel:
    distance_m = _SPEED_PR_DISTANCES_M.get(pr_type)
    record = PersonalRecordModel(
        pr_type=pr_type,
        kind=_personal_record_kind(pr_type),
        rank=raw.rank,
        value=round_or_none(raw.value, 3),
        date=_to_date(raw.workout_date),
        workout_id=raw.workout_id,
        workout_title=raw.workout_title,
    )
    if distance_m is None or not raw.value:
        return record
    return record.model_copy(
        update={
            "time": format_duration(distance_m / raw.value),
            "pace_per_km": format_pace(raw.value),
        },
    )


def _personal_record_kind(pr_type: str) -> PersonalRecordKind:
    if pr_type.startswith("power"):
        return "power"
    if pr_type.startswith("hR"):
        return "heart_rate"
    return "speed"


def _to_event_model(raw: _RawEvent) -> EventModel:
    return EventModel(
        id=raw.id,
        name=raw.name,
        date=_to_date(raw.event_date),
        event_type=raw.event_type,
        priority=raw.atp_priority,
        distance_km=_event_distance_km(raw.distance, raw.distance_units),
        ctl_target=raw.ctl_target,
        description=raw.description or None,
        comment=raw.comment or None,
        goals=compact_json(raw.goals),
        results=compact_json(raw.results),
    )


def _event_distance_km(distance: float | None, units: str | None) -> float | None:
    # O TrainingPeaks devolve a mesma prova como (50000, "Meters"), (50, "Kilometers"),
    # (50000, "") ou (50, None) dependendo do atleta
    if distance is None:
        return None
    normalized_units = (units or "").strip().lower()
    if normalized_units in {"meters", "metres", "meter", "metre", "m"}:
        return round(distance / 1000, 2)
    if normalized_units in {"miles", "mile", "mi"}:
        return round(distance * _MILES_TO_KM, 2)
    if not normalized_units and distance >= _METERS_HEURISTIC_THRESHOLD:
        return round(distance / 1000, 2)
    return round(distance, 2)


def _to_strength_workout_model(raw: _RawStrengthWorkout) -> StrengthWorkoutModel:
    return StrengthWorkoutModel(
        id=str(raw.id),
        date=_to_date(raw.prescribed_date),
        title=raw.title,
        workout_type=raw.workout_type,
        instructions=raw.instructions or None,
        prescribed_duration_min=_seconds_to_minutes(raw.prescribed_duration_in_seconds),
        executed_duration_min=_seconds_to_minutes(raw.executed_duration_in_seconds),
        compliance_state=raw.compliance_state,
        compliance_percent=round_or_none(raw.compliance_percent),
        rpe=raw.rpe,
        feel=raw.feel,
        tss=round_or_none(raw.completed_tss),
        total_sets=raw.snapshot.total_sets,
        completed_sets=raw.snapshot.completed_sets,
        blocks=[
            StrengthBlockModel(
                block_type=block.block_type,
                title=block.title,
                notes=block.coach_notes,
                compliance_percent=round_or_none(block.compliance_percent),
                exercises=[
                    StrengthExerciseModel(
                        exercise=prescription.exercise.title if prescription.exercise else None,
                        notes=prescription.coach_notes,
                        compliance_percent=round_or_none(prescription.compliance_percent),
                        sets=[
                            _to_strength_set_model(strength_set)
                            for strength_set in prescription.sets
                        ],
                    )
                    for prescription in block.prescriptions
                ],
            )
            for block in raw.blocks
        ],
    )


def _to_strength_set_model(raw: _RawStrengthSet) -> StrengthSetModel:
    return StrengthSetModel(
        complete=raw.is_complete,
        prescribed={
            value.parameter: value.prescribed_value
            for value in raw.parameter_values
            if value.parameter and value.prescribed_value is not None
        },
        executed={
            value.parameter: value.executed_value
            for value in raw.parameter_values
            if value.parameter and value.executed_value is not None
        },
    )


def _to_library_workout_model(library_id: int, raw: _RawLibraryItem) -> LibraryWorkoutModel:
    return LibraryWorkoutModel(
        id=raw.exercise_library_item_id,
        library_id=library_id,
        name=raw.item_name,
        sport=_SPORTS_BY_TYPE_ID.get(raw.workout_type_id or 0),
        duration_planned_min=hours_to_minutes(raw.total_time_planned),
        distance_planned_km=meters_to_km(raw.distance_planned),
        tss_planned=round_or_none(raw.tss_planned),
        intensity_factor_planned=round_or_none(raw.if_planned, 2),
        description=raw.description or None,
        coach_comments=raw.coach_comments or None,
        structure=_decode_structure(raw.structure),
    )


def _compact_dict(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    compacted = compact_json(value)
    return compacted if isinstance(compacted, dict) else {}


def _to_date(value: str | None) -> date | None:
    # Datas do TrainingPeaks vêm como "2026-11-08T00:00:00": só a parte da data importa
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _seconds_to_minutes(seconds: float | None) -> float | None:
    if seconds is None:
        return None
    return round(seconds / 60, 1)


def _round_int(value: float | None) -> int | None:
    return round(value) if value is not None else None


def _full_name(first_name: str | None, last_name: str | None) -> str | None:
    name = " ".join(part for part in (first_name, last_name) if part)
    return name or None
