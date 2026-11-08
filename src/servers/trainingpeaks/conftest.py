import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import JsonValue

from src.log import logger

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
ATHLETE_ID = 123


def load_fixture(name: str) -> JsonValue:
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class FakeTrainingPeaksApi:
    """
    API falsa servida por `httpx.MockTransport`.

    `routes` mapeia "METHOD path" → (status, corpo JSON). Rota não cadastrada
    responde 404. Toda requisição recebida fica em `requests`.
    """

    routes: dict[str, tuple[int, JsonValue]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    token_statuses: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.add("GET", "/users/v3/token", load_fixture("token"))
        self.add("GET", "/users/v3/user", load_fixture("user"))

    def add(self, method: str, path: str, body: JsonValue, *, status: int = 200) -> None:
        self.routes[f"{method} {path}"] = (status, body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def requests_to(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/users/v3/token" and self.token_statuses:
            status = self.token_statuses.pop(0)
            if status != httpx.codes.OK:
                return httpx.Response(status)
        status, body = self.routes.get(
            f"{request.method} {request.url.path}",
            (httpx.codes.NOT_FOUND, None),
        )
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)
