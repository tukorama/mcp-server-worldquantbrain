from importlib.metadata import PackageNotFoundError, version

from mcp_server_worldquantbrain.client import BrainClient
from mcp_server_worldquantbrain.config import Settings

try:
    __version__ = version("mcp-server-worldquantbrain")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["BrainClient", "Settings", "__version__"]
