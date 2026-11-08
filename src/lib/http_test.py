import re

import httpx
import pytest

from src.lib.http import (
    AutoRaiseTransport,
    ReadOnlyTransport,
    ReadOnlyViolationError,
    is_retryable_http_error,
)


def _make_sut(handler_status: int = 200) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(handler_status, json={})

    transport = ReadOnlyTransport(
        AutoRaiseTransport(httpx.MockTransport(handler)),
        read_only_post_paths=(re.compile(r"/report/\d+"),),
    )
    return httpx.AsyncClient(base_url="https://api.test", transport=transport), received


@pytest.mark.asyncio
async def test_read_only_transport_allows_get():
    # Arrange
    sut, received = _make_sut()

    # Act
    response = await sut.get("/anything")

    # Assert
    assert response.status_code == 200
    assert len(received) == 1


@pytest.mark.asyncio
async def test_read_only_transport_allows_whitelisted_post():
    # Arrange
    sut, received = _make_sut()

    # Act
    response = await sut.post("/report/42", json={"filter": 1})

    # Assert
    assert response.status_code == 200
    assert len(received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/report/42/delete"), ("PUT", "/report/42"), ("DELETE", "/x"), ("PATCH", "/x")],
)
async def test_read_only_transport_blocks_writes_before_sending(method: str, path: str):
    # Arrange
    sut, received = _make_sut()

    # Act / Assert
    with pytest.raises(ReadOnlyViolationError):
        await sut.request(method, path)
    assert received == []


@pytest.mark.asyncio
async def test_auto_raise_transport_raises_on_error_status():
    # Arrange
    sut, _ = _make_sut(handler_status=503)

    # Act
    with pytest.raises(httpx.HTTPStatusError) as error:
        await sut.get("/anything")

    # Assert
    assert is_retryable_http_error(error.value)
