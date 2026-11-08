import asyncio
import functools
from collections.abc import Awaitable, Callable

from src.log import logger


class InvalidMaxAttemptsError(Exception):
    def __init__(self, max_attempts: int, /) -> None:
        self.max_attempts = max_attempts

    def __str__(self) -> str:
        return f"Invalid max attempts: {self.max_attempts}"


def retry[**P, T](
    *,
    max_attempts: int,
    when: Callable[[Exception], bool] = lambda _: True,
    delay_seconds: float = 0.5,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """
    Retenta a corrotina enquanto `when(erro)` for verdadeiro.

    Na última tentativa (ou com erro não retentável) relança o erro original.
    """
    if max_attempts < 1:
        raise InvalidMaxAttemptsError(max_attempts)

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as error:
                    if not when(error):
                        raise
                    logger.warning(
                        f"{func.__name__}: tentativa {attempt} de {max_attempts} "
                        f"falhou ({type(error).__name__})",
                    )
                    await asyncio.sleep(delay_seconds * attempt)
            return await func(*args, **kwargs)

        return wrapper

    return decorator
