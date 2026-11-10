from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated

from httpx import HTTPError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from src.lib.http import ReadOnlyViolationError
from src.lib.utils import today
from src.log import logger
from src.servers.trainingpeaks.client import TrainingPeaksClient, TrainingPeaksError
from src.servers.trainingpeaks.domain import (
    AthleteProfileModel,
    AtpModel,
    AvailabilityListModel,
    CalendarNoteListModel,
    EquipmentListModel,
    EventListModel,
    FitnessModel,
    HealthMetricsModel,
    LibraryWorkoutListModel,
    LibraryWorkoutModel,
    PersonalRecordListModel,
    PersonalRecordSport,
    StrengthWorkoutListModel,
    StrengthWorkoutModel,
    WeekSummaryModel,
    WorkoutAnalysisModel,
    WorkoutDetailModel,
    WorkoutLibraryListModel,
    WorkoutListModel,
    WorkoutStatusFilter,
)
from src.servers.trainingpeaks.environment import load_auth_cookie
from src.servers.trainingpeaks.service import TrainingPeaksService

_INSTRUCTIONS = """\
Acesso somente leitura ao TrainingPeaks do atleta (treinos, fitness, provas, saúde).
Nenhuma tool cria, altera ou apaga dados.

Fluxo típico: list_workouts para achar o treino → get_workout (detalhe, comentários,
nota privada, recordes) → get_workout_analysis (voltas, canais, zonas, série temporal).

Conceitos:
- TSS = carga do treino. CTL = fitness (média de 42 dias), ATL = fadiga (7 dias),
  TSB = forma (CTL - ATL). TSB positivo = descansado; muito negativo = fadiga alta.
- IF = intensidade relativa ao limiar (1.0 = no limiar).
- Treino "completed" tem dados realizados; sem isso é só planejado (pode estar no passado
  = treino perdido).
- Treinos de força do construtor de força ficam em outra API: use list_strength_workouts.

Unidades: durações em minutos (ou horas quando indicado), distâncias em km, pace em
m:ss por km (corrida) ou por 100 m (natação), potência em W, FC em bpm.
Datas no formato YYYY-MM-DD. Ids vêm das tools de listagem — nunca invente um id.
"""

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_DEFAULT_FITNESS_DAYS = 90
_DEFAULT_PERSONAL_RECORD_YEARS = 10
_DEFAULT_EVENTS_DAYS = 365
_DAYS_PER_YEAR = 365

_StartDate = Annotated[date, Field(description="Data inicial (YYYY-MM-DD), inclusiva")]
_EndDate = Annotated[date, Field(description="Data final (YYYY-MM-DD), inclusiva")]
_OptionalStartDate = Annotated[
    date | None,
    Field(description="Data inicial (YYYY-MM-DD), inclusiva"),
]
_OptionalEndDate = Annotated[
    date | None,
    Field(description="Data final (YYYY-MM-DD), inclusiva. Padrão: hoje"),
]


@dataclass(frozen=True)
class _AppContext:
    client: TrainingPeaksClient
    service: TrainingPeaksService


@asynccontextmanager
async def _lifespan(_server: MCPServer) -> AsyncGenerator[_AppContext]:
    client = TrainingPeaksClient(auth_cookie=load_auth_cookie)
    try:
        yield _AppContext(client=client, service=TrainingPeaksService(client))
    finally:
        await client.aclose()


mcp = MCPServer("trainingpeaks", instructions=_INSTRUCTIONS, lifespan=_lifespan)


# ---------------------------------------------------------------- atleta


@mcp.tool(annotations=_READ_ONLY)
async def get_athlete_profile(ctx: Context) -> AthleteProfileModel:
    """
    Perfil do atleta: dados pessoais, conta (premium), limiares e zonas de
    potência, frequência cardíaca e velocidade/pace por esporte, calorias diárias
    planejadas e piscinas configuradas.
    """
    with _translate_errors():
        return await _app(ctx).service.get_athlete_profile()


# ---------------------------------------------------------------- treinos


@mcp.tool(annotations=_READ_ONLY)
async def list_workouts(
    ctx: Context,
    *,
    start_date: _StartDate,
    end_date: _EndDate,
    status: Annotated[
        WorkoutStatusFilter,
        Field(description="all, planned (sem dados realizados) ou completed"),
    ] = "all",
    sport: Annotated[
        str | None,
        Field(description="Filtra pelo esporte: Run, Bike, Swim, Strength, Walk, ..."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=1000, description="Máximo de treinos")] = 200,
) -> WorkoutListModel:
    """
    Lista treinos (planejados e realizados) de um período, com métricas
    principais e totais realizados por esporte. Não inclui treinos do construtor
    de força (use list_strength_workouts). Para detalhes use get_workout.
    """
    _check_range(start_date, end_date)
    with _translate_errors():
        return await _app(ctx).service.list_workouts(
            start_date,
            end_date,
            status=status,
            sport=sport,
            limit=limit,
        )


@mcp.tool(annotations=_READ_ONLY)
async def get_workout(
    ctx: Context,
    *,
    workout_id: Annotated[int, Field(description="Id do treino (de list_workouts)")],
) -> WorkoutDetailModel:
    """
    Detalhe de um treino: planejado vs realizado, estrutura de intervalos,
    descrição, comentários (atleta/treinador), nota privada, arquivos do
    dispositivo e recordes pessoais batidos nele.
    """
    with _translate_errors():
        return await _app(ctx).service.get_workout(workout_id)


@mcp.tool(annotations=_READ_ONLY)
async def get_workout_analysis(
    ctx: Context,
    *,
    workout_id: Annotated[int, Field(description="Id do treino (de list_workouts)")],
    time_series_points: Annotated[
        int,
        Field(
            ge=0,
            le=2000,
            description="Quantos pontos da série temporal devolver (0 = nenhum)",
        ),
    ] = 0,
) -> WorkoutAnalysisModel:
    """
    Análise de um treino realizado (dados do arquivo do dispositivo): totais
    (NP, IF, TSS, desacoplamento...), estatísticas por canal (FC, potência,
    velocidade, cadência, altitude) com zonas, voltas e, opcionalmente, uma amostra
    da série temporal. Treino sem arquivo pode não ter voltas nem canais.
    """
    with _translate_errors():
        return await _app(ctx).service.get_workout_analysis(
            workout_id,
            time_series_points=time_series_points,
        )


@mcp.tool(annotations=_READ_ONLY)
async def list_strength_workouts(
    ctx: Context,
    *,
    start_date: _StartDate,
    end_date: _EndDate,
) -> StrengthWorkoutListModel:
    """
    Lista treinos de força/academia do construtor de força do TrainingPeaks
    (não aparecem em list_workouts), com aderência e exercícios.
    """
    _check_range(start_date, end_date)
    with _translate_errors():
        workouts = await _app(ctx).client.list_strength_workouts(start_date, end_date)
    return StrengthWorkoutListModel(
        start_date=start_date,
        end_date=end_date,
        workouts=sorted(workouts, key=lambda workout: workout.date or date.max),
    )


@mcp.tool(annotations=_READ_ONLY)
async def get_strength_workout(
    ctx: Context,
    *,
    workout_id: Annotated[str, Field(description="Id de list_strength_workouts")],
) -> StrengthWorkoutModel:
    """
    Detalhe de um treino de força: blocos, exercícios e séries com valores
    prescritos vs executados (repetições, carga, duração...).
    """
    with _translate_errors():
        return await _app(ctx).client.get_strength_workout(workout_id)


# ---------------------------------------------------------------- performance


@mcp.tool(annotations=_READ_ONLY)
async def get_fitness(
    ctx: Context,
    *,
    start_date: _OptionalStartDate = None,
    end_date: _OptionalEndDate = None,
) -> FitnessModel:
    """
    Curva de performance (PMC): TSS, CTL (fitness), ATL (fadiga) e TSB (forma)
    por dia, com o estado atual de forma. Padrão: últimos 90 dias.
    """
    end = end_date or today()
    start = start_date or end - timedelta(days=_DEFAULT_FITNESS_DAYS)
    _check_range(start, end)
    with _translate_errors():
        return await _app(ctx).service.get_fitness(start, end)


@mcp.tool(annotations=_READ_ONLY)
async def get_week_summary(
    ctx: Context,
    *,
    week_of: Annotated[
        date | None,
        Field(description="Qualquer data da semana (seg-dom). Padrão: semana atual"),
    ] = None,
) -> WeekSummaryModel:
    """
    Resumo de uma semana: treinos planejados vs realizados, TSS e horas,
    totais por esporte, treinos de força e fitness (CTL/ATL/TSB) no fim da semana.
    """
    with _translate_errors():
        return await _app(ctx).service.get_week_summary(week_of or today())


@mcp.tool(annotations=_READ_ONLY)
async def get_personal_records(
    ctx: Context,
    *,
    sport: Annotated[PersonalRecordSport, Field(description="Bike ou Run")],
    pr_type: Annotated[
        str | None,
        Field(
            description=(
                "Tipo do recorde. Bike: power5sec, power1min, power5min, power10min, "
                "power20min, power60min, power90min. Bike e Run: hR5sec ... hR90min. "
                "Run: speed400Meter, speed800Meter, speed1K, speed1Mi, speed5K, "
                "speed5Mi, speed10K, speed10Mi, speedHalfMarathon, speedMarathon, "
                "speed50K. Vazio = o melhor de cada tipo"
            ),
        ),
    ] = None,
    start_date: Annotated[
        date | None,
        Field(description="Data inicial (YYYY-MM-DD). Padrão: 10 anos atrás"),
    ] = None,
    end_date: _OptionalEndDate = None,
    top: Annotated[
        int,
        Field(ge=1, le=50, description="Quantos recordes do ranking (com pr_type)"),
    ] = 5,
) -> PersonalRecordListModel:
    """
    Recordes pessoais (melhores esforços) de potência, FC e tempos de corrida
    num período. Com pr_type devolve o ranking daquele tipo; sem, o melhor de cada.
    """
    end = end_date or today()
    start = start_date or end - timedelta(days=_DEFAULT_PERSONAL_RECORD_YEARS * _DAYS_PER_YEAR)
    _check_range(start, end)
    with _translate_errors():
        return await _app(ctx).service.list_personal_records(
            sport,
            start,
            end,
            pr_type=pr_type,
            top=top,
        )


@mcp.tool(annotations=_READ_ONLY)
async def get_annual_training_plan(
    ctx: Context,
    *,
    start_date: _StartDate,
    end_date: _EndDate,
) -> AtpModel:
    """
    Plano anual de treino (ATP): TSS semanal planejado, período (Base, Build,
    Peak, Race...), provas e semanas até a próxima prova prioritária.
    """
    _check_range(start_date, end_date)
    with _translate_errors():
        weeks = await _app(ctx).client.list_atp_weeks(start_date, end_date)
    return AtpModel(start_date=start_date, end_date=end_date, weeks=weeks)


# ---------------------------------------------------------------- calendário


@mcp.tool(annotations=_READ_ONLY)
async def get_events(
    ctx: Context,
    *,
    start_date: Annotated[
        date | None,
        Field(description="Data inicial (YYYY-MM-DD). Padrão: hoje"),
    ] = None,
    end_date: Annotated[
        date | None,
        Field(description="Data final (YYYY-MM-DD). Padrão: daqui a 1 ano"),
    ] = None,
) -> EventListModel:
    """
    Provas/eventos do calendário com prioridade (A/B/C), distância, meta de CTL,
    objetivos e resultados; inclui sempre a prova em foco e a próxima prova.
    """
    start = start_date or today()
    end = end_date or start + timedelta(days=_DEFAULT_EVENTS_DAYS)
    _check_range(start, end)
    with _translate_errors():
        return await _app(ctx).service.get_events(start, end)


@mcp.tool(annotations=_READ_ONLY)
async def list_calendar_notes(
    ctx: Context,
    *,
    start_date: _StartDate,
    end_date: _EndDate,
) -> CalendarNoteListModel:
    """
    Notas do calendário (ex.: viagem, descanso, recados do treinador) com seus
    comentários.
    """
    _check_range(start_date, end_date)
    with _translate_errors():
        return await _app(ctx).service.list_calendar_notes(start_date, end_date)


@mcp.tool(annotations=_READ_ONLY)
async def list_availability(
    ctx: Context,
    *,
    start_date: _StartDate,
    end_date: _EndDate,
) -> AvailabilityListModel:
    """
    Períodos de indisponibilidade ou disponibilidade limitada (viagem, lesão...)
    marcados no calendário, com os esportes ainda possíveis.
    """
    _check_range(start_date, end_date)
    with _translate_errors():
        entries = await _app(ctx).client.list_availability(start_date, end_date)
    return AvailabilityListModel(start_date=start_date, end_date=end_date, entries=entries)


# ---------------------------------------------------------------- saúde e equipamentos


@mcp.tool(annotations=_READ_ONLY)
async def get_health_metrics(
    ctx: Context,
    *,
    start_date: _StartDate,
    end_date: _EndDate,
) -> HealthMetricsModel:
    """
    Métricas diárias de saúde registradas (peso, HRV, sono, FC de repouso,
    passos, SpO2, lesão...) e dados de nutrição do período.
    """
    _check_range(start_date, end_date)
    with _translate_errors():
        return await _app(ctx).service.get_health_metrics(start_date, end_date)


@mcp.tool(annotations=_READ_ONLY)
async def list_equipment(
    ctx: Context,
    *,
    include_retired: Annotated[
        bool,
        Field(description="Incluir bikes e tênis aposentados"),
    ] = False,
) -> EquipmentListModel:
    """
    Bikes e tênis cadastrados: distância total (já somando a distância inicial), limite de
    troca, se é o padrão e se está aposentado. Qual tênis/bike foi usado em cada treino vem
    em `shoe`/`bike` de list_workouts, get_workout e get_week_summary.
    """
    with _translate_errors():
        equipment = await _app(ctx).client.list_equipment()
    return EquipmentListModel(
        equipment=[item for item in equipment if include_retired or not item.retired],
    )


# ---------------------------------------------------------------- biblioteca


@mcp.tool(annotations=_READ_ONLY)
async def list_workout_libraries(ctx: Context) -> WorkoutLibraryListModel:
    """Bibliotecas de treinos-modelo do atleta (as próprias e as compartilhadas)."""
    with _translate_errors():
        libraries = await _app(ctx).client.list_workout_libraries()
    return WorkoutLibraryListModel(libraries=libraries)


@mcp.tool(annotations=_READ_ONLY)
async def list_library_workouts(
    ctx: Context,
    *,
    library_id: Annotated[int, Field(description="Id de list_workout_libraries")],
) -> LibraryWorkoutListModel:
    """
    Treinos-modelo de uma biblioteca (sem a estrutura de intervalos; para ela
    use get_library_workout).
    """
    with _translate_errors():
        workouts = await _app(ctx).client.list_library_workouts(library_id)
    return LibraryWorkoutListModel(
        library_id=library_id,
        workouts=[workout.model_copy(update={"structure": None}) for workout in workouts],
    )


@mcp.tool(annotations=_READ_ONLY)
async def get_library_workout(
    ctx: Context,
    *,
    library_id: Annotated[int, Field(description="Id de list_workout_libraries")],
    workout_id: Annotated[int, Field(description="Id de list_library_workouts")],
) -> LibraryWorkoutModel:
    """Treino-modelo completo, com a estrutura de intervalos."""
    with _translate_errors():
        return await _app(ctx).client.get_library_workout(library_id, workout_id)


# ---------------------------------------------------------------- helpers


def _app(ctx: Context) -> _AppContext:
    app = ctx.request_context.lifespan_context
    if not isinstance(app, _AppContext):
        msg = "Servidor TrainingPeaks não inicializado"
        raise ToolError(msg)
    return app


def _check_range(start: date, end: date) -> None:
    if start > end:
        msg = f"start_date ({start}) é depois de end_date ({end})"
        raise ToolError(msg)


@contextmanager
def _translate_errors() -> Generator[None]:
    # Só ToolError chega ao LLM com a mensagem; o resto vira "erro inesperado"
    try:
        yield
    except TrainingPeaksError as error:
        raise ToolError(str(error)) from error
    except ReadOnlyViolationError as error:
        logger.error(str(error))
        raise ToolError(str(error)) from error
    except HTTPError as error:
        logger.exception("Falha na API do TrainingPeaks")
        msg = f"Falha ao falar com a API do TrainingPeaks ({type(error).__name__}). Tente de novo."
        raise ToolError(msg) from error
