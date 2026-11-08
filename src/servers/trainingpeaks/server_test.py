import pytest
from mcp import Client

from src.servers.trainingpeaks.server import mcp


@pytest.mark.asyncio
async def test_every_tool_is_read_only():
    # Arrange
    async with Client(mcp) as sut:
        # Act
        result = await sut.list_tools()

    # Assert
    assert len(result.tools) == 18
    for tool in result.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint, tool.name
        assert tool.description, tool.name


@pytest.mark.asyncio
async def test_invalid_date_range_returns_tool_error():
    # Arrange
    async with Client(mcp) as sut:
        # Act
        result = await sut.call_tool(
            "list_workouts",
            {"start_date": "2026-11-08", "end_date": "2026-11-01"},
        )

    # Assert
    assert result.is_error
    assert "é depois de end_date" in str(result.content)
