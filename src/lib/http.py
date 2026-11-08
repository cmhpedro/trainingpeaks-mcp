import re
from collections.abc import Collection

from httpx import (
    AsyncBaseTransport,
    AsyncHTTPTransport,
    HTTPStatusError,
    Request,
    Response,
    TransportError,
)

from src.log import logger

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ReadOnlyViolationError(Exception):
    def __init__(self, method: str, path: str, /) -> None:
        self.method = method
        self.path = path

    def __str__(self) -> str:
        return f"Requisição de escrita bloqueada: {self.method} {self.path}"


class AutoRaiseTransport(AsyncBaseTransport):
    """Transforma resposta >= 400 em `HTTPStatusError`."""

    def __init__(self, inner: AsyncBaseTransport | None = None) -> None:
        self._inner = inner or AsyncHTTPTransport()

    async def handle_async_request(self, request: Request) -> Response:
        response = await self._inner.handle_async_request(request)
        response.request = request
        if response.is_error:
            await response.aread()
            logger.error(f"{request.method} {request.url.path} → {response.status_code}")
            response.raise_for_status()
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class ReadOnlyTransport(AsyncBaseTransport):
    """
    Bloqueia qualquer escrita antes de sair da máquina.

    Só passam métodos de leitura e `POST` para os paths de consulta liberados
    (APIs que usam POST para leitura, ex.: relatórios com filtros no corpo).
    """

    def __init__(
        self,
        inner: AsyncBaseTransport,
        *,
        read_only_post_paths: Collection[re.Pattern[str]],
    ) -> None:
        self._inner = inner
        self._read_only_post_paths = read_only_post_paths

    async def handle_async_request(self, request: Request) -> Response:
        method = request.method.upper()
        path = request.url.path
        is_read_only_post = method == "POST" and any(
            pattern.fullmatch(path) for pattern in self._read_only_post_paths
        )
        if method not in _READ_METHODS and not is_read_only_post:
            raise ReadOnlyViolationError(method, path)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def is_retryable_http_error(error: Exception, /) -> bool:
    if isinstance(error, TransportError):
        return True
    return isinstance(error, HTTPStatusError) and error.response.status_code in _RETRYABLE_STATUS
