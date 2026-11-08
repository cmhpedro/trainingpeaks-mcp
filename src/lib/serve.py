from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.log import logger

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class _ServeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_", extra="ignore")

    TRANSPORT: Literal["stdio", "streamable-http"] = "stdio"
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    # Hostnames públicos (ex.: o do proxy na frente do servidor), separados por vírgula
    ALLOWED_HOSTS: str = Field(default="")


def run(mcp: MCPServer) -> None:
    """
    Sobe o servidor em stdio (padrão, uso local) ou streamable HTTP (deploy).

    Em HTTP o servidor não autentica: precisa ficar atrás de um proxy com login
    (ex.: Cloudflare Access) antes de a requisição chegar aqui.
    """
    settings = _ServeSettings()
    if settings.TRANSPORT == "stdio":
        mcp.run()
        return

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def _health(_request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]  # ruff: ignore[unused-async] - o Starlette exige handler async
        return JSONResponse({"status": "ok"})

    public_hosts = [host.strip() for host in settings.ALLOWED_HOSTS.split(",") if host.strip()]
    logger.info(f"MCP em http://{settings.HOST}:{settings.PORT}/mcp para {public_hosts}")
    mcp.run(
        "streamable-http",
        host=settings.HOST,
        port=settings.PORT,
        # Sem sessão no servidor: reinício do container não derruba os clientes
        stateless_http=True,
        transport_security=_transport_security(public_hosts),
    )


def _transport_security(public_hosts: list[str]) -> TransportSecuritySettings:
    # Proteção contra DNS rebinding: só aceita Host/Origin conhecidos
    hosts = [*_LOOPBACK_HOSTS, *public_hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, *(f"{host}:*" for host in hosts)],
        allowed_origins=[
            f"{scheme}://{host}{port}"
            for scheme in ("http", "https")
            for host in hosts
            for port in ("", ":*")
        ],
    )
