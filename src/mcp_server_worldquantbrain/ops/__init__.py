from importlib import import_module

from mcp_server_worldquantbrain.ops.registry import (
    OPERATIONS,
    Call,
    Operation,
    Runtime,
    enabled_operations,
)

for _module in ("alphas", "auth", "data", "simulations", "submit", "users"):
    import_module(f"{__name__}.{_module}")

__all__ = ["OPERATIONS", "Call", "Operation", "Runtime", "enabled_operations"]
