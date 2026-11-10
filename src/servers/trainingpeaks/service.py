import asyncio
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from src.lib.utils import downsample, split_date_range
from src.log import logger
from src.servers.trainingpeaks.client import (
    TrainingPeaksClient,
    personal_record_types,
)
from src.servers.trainingpeaks.domain import (
    AthleteProfileModel,
    CalendarNoteListModel,
    EventListModel,
    FitnessDayModel,
    FitnessModel,
    HealthMetricsModel,
    PersonalRecordListModel,
    PersonalRecordSport,
    SportTotalsModel,
    WeekSummaryModel,
    WorkoutAnalysisModel,
    WorkoutDetailModel,
    WorkoutListModel,
    WorkoutStatusFilter,
    WorkoutSummaryModel,
)

# A API de treinos responde mal a intervalos longos: consulta em janelas
_WORKOUTS_WINDOW_DAYS = 90
_DAYS_PER_WEEK = 7

# Faixas de TSB (forma) usadas pela comunidade do TrainingPeaks
_FORM_STATUS = (
    (25, "Muito descansado (risco de destreino)"),
    (10, "Descansado (pronto para prova)"),
    (0, "Neutro (treino normal)"),
    (-10, "Cansado (absorvendo treino)"),
    (-25, "Muito cansado (fadiga alta)"),
)
_FORM_STATUS_OVERREACHING = "Exausto (risco de overreaching)"


class TrainingPeaksService:
    def __init__(self, client: TrainingPeaksClient) -> None:
        self._client = client

    async def get_athlete_profile(self) -> AthleteProfileModel:
        account = await self._client.get_account()
        settings, pool_lengths = await asyncio.gather(
            self._client.get_athlete_settings(),
            self._client.list_pool_lengths(),
        )
        return AthleteProfileModel(
            athlete_id=account.athlete_id,
            name=account.name or settings.full_name or "",
            email=account.email or settings.email,
            is_premium=account.is_premium,
            gender=settings.gender,
            birthday=settings.birthday,
            age=settings.age,
            city=settings.city,
            timezone=settings.timezone,
            account_expires_on=settings.account_expires_on,
            planned_daily_calories=settings.planned_daily_calories,
            power_zones=settings.power_zones,
            heart_rate_zones=settings.heart_rate_zones,
            speed_zones=settings.speed_zones,
            pool_lengths=pool_lengths,
        )

    async def list_workouts(
        self,
        start: date,
        end: date,
        *,
        status: WorkoutStatusFilter,
        sport: str | None,
        limit: int,
    ) -> WorkoutListModel:
        workouts = await self._fetch_workouts(start, end)
        if status == "planned":
            workouts = [workout for workout in workouts if not workout.completed]
        elif status == "completed":
            workouts = [workout for workout in workouts if workout.completed]
        if sport is not None:
            workouts = [
                workout
                for workout in workouts
                if (workout.sport or "").casefold() == sport.casefold()
            ]
        return WorkoutListModel(
            start_date=start,
            end_date=end,
            count=len(workouts),
            truncated=len(workouts) > limit,
            totals=_completed_totals_by_sport(workouts),
            workouts=await self._with_equipment_names(workouts[:limit]),
        )

    async def get_workout(self, workout_id: int) -> WorkoutDetailModel:
        [workout] = await self._with_equipment_names([await self._client.get_workout(workout_id)])
        files, personal_records = await asyncio.gather(
            self._client.list_workout_files(workout_id),
            self._client.list_workout_personal_records(workout_id),
        )
        private_note = (
            await self._client.get_private_note(workout_id) if workout.has_private_note else None
        )
        return workout.model_copy(
            update={
                "files": files,
                "personal_records": personal_records,
                "private_note": private_note,
            },
        )

    async def get_workout_analysis(
        self,
        workout_id: int,
        *,
        time_series_points: int,
    ) -> WorkoutAnalysisModel:
        analysis = await self._client.get_workout_analysis(workout_id)
        return analysis.model_copy(
            update={
                "time_series": downsample(analysis.time_series, max_items=time_series_points),
            },
        )

    async def list_personal_records(
        self,
        sport: PersonalRecordSport,
        start: date,
        end: date,
        *,
        pr_type: str | None,
        top: int,
    ) -> PersonalRecordListModel:
        if pr_type is not None:
            records = await self._client.list_personal_records(sport, pr_type, start, end)
            return PersonalRecordListModel(
                sport=sport,
                start_date=start,
                end_date=end,
                records=records[:top],
            )

        # Sem tipo: o melhor de cada tipo (uma chamada por tipo)
        records_by_type = [
            await self._client.list_personal_records(sport, each_type, start, end)
            for each_type in personal_record_types(sport)
        ]
        return PersonalRecordListModel(
            sport=sport,
            start_date=start,
            end_date=end,
            records=[records[0] for records in records_by_type if records],
        )

    async def get_fitness(self, start: date, end: date) -> FitnessModel:
        days = await self._client.list_fitness(start, end)
        current = days[-1] if days else None
        return FitnessModel(
            start_date=start,
            end_date=end,
            current=current,
            form_status=_form_status(current.tsb) if current else None,
            days=days,
        )

    async def get_week_summary(self, week_of: date) -> WeekSummaryModel:
        monday = week_of - timedelta(days=week_of.weekday())
        sunday = monday + timedelta(days=_DAYS_PER_WEEK - 1)
        workouts, fitness, strength_workouts = await asyncio.gather(
            self._client.list_workouts(monday, sunday),
            self._client.list_fitness(monday, sunday),
            self._client.list_strength_workouts(monday, sunday),
        )
        workouts = await self._with_equipment_names(sorted(workouts, key=_workout_sort_key))
        return WeekSummaryModel(
            start_date=monday,
            end_date=sunday,
            planned_count=sum(1 for workout in workouts if workout.duration_planned_min),
            completed_count=sum(1 for workout in workouts if workout.completed),
            planned_tss=_sum(workout.tss_planned for workout in workouts),
            completed_tss=_sum(workout.tss for workout in workouts),
            planned_duration_hours=_minutes_to_hours(
                _sum(workout.duration_planned_min for workout in workouts),
            ),
            completed_duration_hours=_minutes_to_hours(
                _sum(workout.duration_min for workout in workouts),
            ),
            totals=_completed_totals_by_sport(workouts),
            fitness_at_end=_last_until(fitness, sunday),
            workouts=workouts,
            strength_workouts=strength_workouts,
        )

    async def get_events(self, start: date, end: date) -> EventListModel:
        focus_event, next_event, events = await asyncio.gather(
            self._client.get_focus_event(),
            self._client.get_next_event(),
            self._client.list_events(start, end),
        )
        return EventListModel(
            focus_event=focus_event,
            next_event=next_event,
            start_date=start,
            end_date=end,
            events=sorted(events, key=lambda event: event.date or date.max),
        )

    async def list_calendar_notes(self, start: date, end: date) -> CalendarNoteListModel:
        notes = await self._client.list_calendar_notes(start, end)
        notes_with_comments = [
            note.model_copy(
                update={"comments": await self._client.list_note_comments(note.id)},
            )
            if note.comment_count
            else note
            for note in notes
        ]
        return CalendarNoteListModel(start_date=start, end_date=end, notes=notes_with_comments)

    async def get_health_metrics(self, start: date, end: date) -> HealthMetricsModel:
        metrics, nutrition = await asyncio.gather(
            self._client.list_health_metrics(start, end),
            self._client.list_nutrition(start, end),
        )
        return HealthMetricsModel(
            start_date=start,
            end_date=end,
            metrics=sorted(metrics, key=lambda entry: str(entry.timestamp)),
            nutrition=nutrition,
        )

    async def _with_equipment_names[W: WorkoutSummaryModel](self, workouts: list[W]) -> list[W]:
        # O treino só traz o id do tênis/bike; o nome vem da lista de equipamentos
        if not any(workout.shoe_id or workout.bike_id for workout in workouts):
            return workouts
        names = {item.id: item.name for item in await self._client.list_equipment()}
        return [
            workout.model_copy(
                update={
                    "shoe": _equipment_name(names, workout.shoe_id),
                    "bike": _equipment_name(names, workout.bike_id),
                },
            )
            for workout in workouts
        ]

    async def _fetch_workouts(self, start: date, end: date) -> list[WorkoutSummaryModel]:
        windows = split_date_range(start, end, max_days=_WORKOUTS_WINDOW_DAYS)
        workouts_by_window = [
            await self._client.list_workouts(window_start, window_end)
            for window_start, window_end in windows
        ]
        unique = {workout.id: workout for workouts in workouts_by_window for workout in workouts}
        return sorted(unique.values(), key=_workout_sort_key)


def _completed_totals_by_sport(workouts: list[WorkoutSummaryModel]) -> list[SportTotalsModel]:
    grouped: defaultdict[str, list[WorkoutSummaryModel]] = defaultdict(list)
    for workout in workouts:
        if workout.completed:
            grouped[workout.sport or "Unknown"].append(workout)
    return [
        SportTotalsModel(
            sport=sport,
            count=len(sport_workouts),
            duration_hours=_minutes_to_hours(
                _sum(workout.duration_min for workout in sport_workouts),
            ),
            distance_km=_sum((workout.distance_km for workout in sport_workouts), digits=2),
            tss=_sum(workout.tss for workout in sport_workouts),
        )
        for sport, sport_workouts in sorted(grouped.items())
    ]


def _equipment_name(names: dict[int, str | None], equipment_id: int | None) -> str | None:
    return names.get(equipment_id) if equipment_id is not None else None


def _workout_sort_key(workout: WorkoutSummaryModel) -> tuple[date, str]:
    return workout.date, str(workout.start_time or "")


def _last_until(days: list[FitnessDayModel], limit: date) -> FitnessDayModel | None:
    eligible = [day for day in days if day.date <= limit]
    return eligible[-1] if eligible else None


def _form_status(tsb: float) -> str:
    for minimum, status in _FORM_STATUS:
        if tsb > minimum:
            return status
    return _FORM_STATUS_OVERREACHING


def _sum(values: Iterable[float | None], *, digits: int = 1) -> float:
    return round(sum(value or 0 for value in values), digits)


def _minutes_to_hours(minutes: float) -> float:
    return round(minutes / 60, 2)
