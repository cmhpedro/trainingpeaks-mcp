from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, JsonValue

from src.log import logger

type PersonalRecordSport = Literal["Bike", "Run"]
type PersonalRecordKind = Literal["power", "heart_rate", "speed"]
type WorkoutStatusFilter = Literal["all", "planned", "completed"]
type EquipmentKind = Literal["bike", "shoe", "other"]
type AvailabilityKind = Literal["unavailable", "limited"]

# ---------------------------------------------------------------- perfil


class AccountModel(BaseModel):
    athlete_id: int
    name: str
    email: str | None
    is_premium: bool


class ZoneModel(BaseModel):
    label: str | None
    minimum: float
    maximum: float
    minimum_pace: str | None = Field(
        default=None,
        description="Só em zonas de velocidade: pace equivalente ao mínimo",
    )
    maximum_pace: str | None = Field(
        default=None,
        description="Só em zonas de velocidade: pace equivalente ao máximo",
    )


class ZoneSetModel(BaseModel):
    sport: str = Field(description="Esporte da zona (Default vale para os demais)")
    unit: str = Field(description="Unidade de threshold/minimum/maximum")
    threshold: float | None
    threshold_pace: str | None = None
    maximum_heart_rate: int | None = None
    resting_heart_rate: int | None = None
    calculation_method: int | None = Field(
        default=None,
        description="Id do método de cálculo no TrainingPeaks",
    )
    zones: list[ZoneModel]


class AthleteSettingsModel(BaseModel):
    full_name: str | None
    email: str | None
    gender: str | None
    birthday: date | None
    age: int | None
    city: str | None
    timezone: str | None
    account_expires_on: date | None
    planned_daily_calories: int | None
    power_zones: list[ZoneSetModel]
    heart_rate_zones: list[ZoneSetModel]
    speed_zones: list[ZoneSetModel]


class PoolLengthModel(BaseModel):
    length: float
    unit: str
    is_default: bool


class AthleteProfileModel(BaseModel):
    athlete_id: int
    name: str
    email: str | None
    is_premium: bool
    gender: str | None
    birthday: date | None
    age: int | None
    city: str | None
    timezone: str | None
    account_expires_on: date | None
    planned_daily_calories: int | None
    power_zones: list[ZoneSetModel]
    heart_rate_zones: list[ZoneSetModel]
    speed_zones: list[ZoneSetModel]
    pool_lengths: list[PoolLengthModel]


# ---------------------------------------------------------------- treinos


class WorkoutSummaryModel(BaseModel):
    id: int
    date: date
    start_time: datetime | None = Field(description="Horário local de início, se houver")
    title: str | None
    sport: str | None
    completed: bool
    duration_planned_min: float | None
    duration_min: float | None
    distance_planned_km: float | None
    distance_km: float | None
    tss_planned: float | None
    tss: float | None
    intensity_factor: float | None
    average_heart_rate_bpm: int | None
    average_power_w: float | None
    normalized_power_w: float | None
    average_pace_per_km: str | None = Field(description="m:ss por km (corrida/caminhada)")
    average_pace_per_100m: str | None = Field(description="m:ss por 100 m (natação)")
    average_speed_kmh: float | None
    elevation_gain_m: float | None
    calories_kcal: float | None
    rpe: int | None = Field(description="Esforço percebido (1-10)")
    feeling: int | None = Field(description="Sensação registrada pelo atleta (0-10)")
    description: str | None = Field(description="Descrição/instruções do treino")


class WorkoutCommentModel(BaseModel):
    author: str | None
    is_coach: bool
    created_at: datetime | None
    comment: str


class WorkoutFileModel(BaseModel):
    name: str | None
    uploaded_at: datetime | None


class WorkoutPersonalRecordModel(BaseModel):
    kind: str | None = Field(description="Power, HeartRate ou Speed")
    type: str | None
    timeframe: str | None
    rank: int | None
    value: float | None


class WorkoutDetailModel(WorkoutSummaryModel):
    intensity_factor_planned: float | None
    maximum_heart_rate_bpm: int | None
    maximum_power_w: float | None
    average_cadence: float | None
    maximum_speed_kmh: float | None
    elevation_loss_m: float | None
    coach_comments: str | None
    has_private_note: bool
    structure: JsonValue = Field(
        description="Estrutura planejada (blocos/intervalos) no formato do TrainingPeaks",
    )
    comments: list[WorkoutCommentModel]
    private_note: str | None = None
    files: list[WorkoutFileModel] = Field(default_factory=list)
    personal_records: list[WorkoutPersonalRecordModel] = Field(default_factory=list)


class SportTotalsModel(BaseModel):
    sport: str
    count: int
    duration_hours: float
    distance_km: float
    tss: float


class WorkoutListModel(BaseModel):
    start_date: date
    end_date: date
    count: int
    truncated: bool = Field(description="True se a lista foi cortada pelo limit")
    totals: list[SportTotalsModel] = Field(
        description="Totais realizados por esporte (sobre todos os treinos, não só a página)",
    )
    workouts: list[WorkoutSummaryModel]


# ---------------------------------------------------------------- análise


class AnalysisTotalModel(BaseModel):
    name: str
    value: JsonValue
    unit: str | None


class AnalysisZoneModel(BaseModel):
    label: str | None
    minimum: float | None
    maximum: float | None


class AnalysisChannelModel(BaseModel):
    identifier: str
    name: str | None
    unit: str | None
    minimum: float | None
    maximum: float | None
    average: float | None
    zones: list[AnalysisZoneModel]


class AnalysisLapColumnModel(BaseModel):
    identifier: str
    name: str | None
    unit: str | None


class WorkoutAnalysisModel(BaseModel):
    workout_id: int
    start_time: datetime | None
    totals: list[AnalysisTotalModel]
    channels: list[AnalysisChannelModel]
    lap_columns: list[AnalysisLapColumnModel]
    laps: list[dict[str, JsonValue]]
    time_series_total_points: int
    time_series: list[dict[str, JsonValue]] = Field(
        description="Amostra igualmente espaçada da série temporal",
    )


# ---------------------------------------------------------------- recordes


class PersonalRecordModel(BaseModel):
    pr_type: str
    kind: PersonalRecordKind
    rank: int | None
    value: float | None = Field(description="W (power), bpm (heart_rate) ou m/s (speed)")
    date: date | None
    workout_id: int | None
    workout_title: str | None
    time: str | None = Field(default=None, description="Só speed: tempo na distância")
    pace_per_km: str | None = Field(default=None, description="Só speed")


class PersonalRecordListModel(BaseModel):
    sport: PersonalRecordSport
    start_date: date
    end_date: date
    records: list[PersonalRecordModel]


# ---------------------------------------------------------------- fitness


class FitnessDayModel(BaseModel):
    date: date
    tss: float
    ctl: float = Field(description="Fitness (carga crônica)")
    atl: float = Field(description="Fadiga (carga aguda)")
    tsb: float = Field(description="Forma (CTL - ATL)")


class FitnessModel(BaseModel):
    start_date: date
    end_date: date
    current: FitnessDayModel | None
    form_status: str | None
    days: list[FitnessDayModel]


class AtpWeekModel(BaseModel):
    week_start: date | None
    planned_tss: float | None
    period: str | None
    race_name: str | None
    race_priority: str | None
    weeks_to_priority_event: int | None


class AtpModel(BaseModel):
    start_date: date
    end_date: date
    weeks: list[AtpWeekModel]


# ---------------------------------------------------------------- calendário


class EventModel(BaseModel):
    id: int
    name: str | None
    date: date | None
    event_type: str | None
    priority: str | None = Field(description="A, B ou C")
    distance_km: float | None
    ctl_target: float | None
    description: str | None
    comment: str | None
    goals: JsonValue
    results: JsonValue


class EventListModel(BaseModel):
    focus_event: EventModel | None = Field(description="Prova A em foco")
    next_event: EventModel | None = Field(description="Próxima prova planejada")
    start_date: date
    end_date: date
    events: list[EventModel]


class NoteCommentModel(BaseModel):
    author: str | None
    created_at: datetime | None
    comment: str


class CalendarNoteModel(BaseModel):
    id: int
    date: date | None
    title: str | None
    description: str | None
    is_hidden: bool
    comment_count: int
    comments: list[NoteCommentModel] = Field(default_factory=list)


class CalendarNoteListModel(BaseModel):
    start_date: date
    end_date: date
    notes: list[CalendarNoteModel]


class AvailabilityModel(BaseModel):
    id: int
    start_date: date | None
    end_date: date | None
    kind: AvailabilityKind
    available_sports: list[str] = Field(description="Só em limited: esportes ainda possíveis")
    reason: str | None
    description: str | None


class AvailabilityListModel(BaseModel):
    start_date: date
    end_date: date
    entries: list[AvailabilityModel]


# ---------------------------------------------------------------- saúde


class MetricValueModel(BaseModel):
    label: str
    value: JsonValue


class HealthMetricEntryModel(BaseModel):
    timestamp: datetime | None
    values: list[MetricValueModel]


class HealthMetricsModel(BaseModel):
    start_date: date
    end_date: date
    metrics: list[HealthMetricEntryModel]
    nutrition: list[JsonValue]


# ---------------------------------------------------------------- equipamentos


class EquipmentModel(BaseModel):
    id: int
    name: str | None
    kind: EquipmentKind
    brand: str | None
    model: str | None
    distance_km: float | None = Field(description="Distância acumulada")
    max_distance_km: float | None = Field(description="Limite de troca configurado")
    retired: bool
    is_default: bool
    purchase_date: date | None
    notes: str | None


class EquipmentListModel(BaseModel):
    equipment: list[EquipmentModel]


# ---------------------------------------------------------------- força


class StrengthWorkoutSummaryModel(BaseModel):
    id: str
    date: date | None
    title: str | None
    workout_type: str | None
    prescribed_duration_min: float | None
    compliance_state: str | None
    compliance_percent: float | None
    total_sets: int | None
    completed_sets: int | None
    exercises: list[str]


class StrengthWorkoutListModel(BaseModel):
    start_date: date
    end_date: date
    workouts: list[StrengthWorkoutSummaryModel]


class StrengthSetModel(BaseModel):
    complete: bool
    prescribed: dict[str, JsonValue] = Field(description="Ex.: Reps, WeightKg, Duration")
    executed: dict[str, JsonValue]


class StrengthExerciseModel(BaseModel):
    exercise: str | None
    notes: str | None
    compliance_percent: float | None
    sets: list[StrengthSetModel]


class StrengthBlockModel(BaseModel):
    block_type: str | None
    title: str | None
    notes: str | None
    compliance_percent: float | None
    exercises: list[StrengthExerciseModel]


class StrengthWorkoutModel(BaseModel):
    id: str
    date: date | None
    title: str | None
    workout_type: str | None
    instructions: str | None
    prescribed_duration_min: float | None
    executed_duration_min: float | None
    compliance_state: str | None
    compliance_percent: float | None
    rpe: int | None
    feel: int | None
    tss: float | None
    total_sets: int | None
    completed_sets: int | None
    blocks: list[StrengthBlockModel]


# ---------------------------------------------------------------- biblioteca


class WorkoutLibraryModel(BaseModel):
    id: int
    name: str | None
    owner_name: str | None
    is_default: bool


class WorkoutLibraryListModel(BaseModel):
    libraries: list[WorkoutLibraryModel]


class LibraryWorkoutModel(BaseModel):
    id: int
    library_id: int
    name: str | None
    sport: str | None
    duration_planned_min: float | None
    distance_planned_km: float | None
    tss_planned: float | None
    intensity_factor_planned: float | None
    description: str | None
    coach_comments: str | None
    structure: JsonValue = Field(default=None, description="Estrutura de intervalos")


class LibraryWorkoutListModel(BaseModel):
    library_id: int
    workouts: list[LibraryWorkoutModel]


# ---------------------------------------------------------------- semana


class WeekSummaryModel(BaseModel):
    start_date: date
    end_date: date
    planned_count: int
    completed_count: int
    planned_tss: float
    completed_tss: float
    planned_duration_hours: float
    completed_duration_hours: float
    totals: list[SportTotalsModel]
    fitness_at_end: FitnessDayModel | None
    workouts: list[WorkoutSummaryModel]
    strength_workouts: list[StrengthWorkoutSummaryModel]
