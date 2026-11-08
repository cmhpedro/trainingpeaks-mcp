from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.log import logger

# O Claude Code sobe o servidor com outro diretório de trabalho: o .env é
# resolvido pela raiz do repo, não pelo cwd.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class TrainingPeaksSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRAININGPEAKS_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Valor do cookie `Production_tpAuth` de app.trainingpeaks.com. Vazio é
    # tratado pelo client como erro de autenticação, com instruções para o LLM.
    AUTH_COOKIE: SecretStr = SecretStr("")


def load_auth_cookie() -> str:
    """Lê o cookie a cada chamada: trocar o .env não exige reiniciar o servidor."""
    settings = TrainingPeaksSettings()
    return settings.AUTH_COOKIE.get_secret_value()
