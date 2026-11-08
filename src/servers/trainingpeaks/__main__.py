from src.lib.serve import run
from src.log import logger
from src.servers.trainingpeaks.server import mcp

run(mcp)
