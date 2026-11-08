import sys

from loguru import logger

# Em stdio o stdout é o canal do protocolo MCP: todo log vai para o stderr.
logger.remove()
logger.add(sys.stderr, level="INFO")

__all__ = ["logger"]
