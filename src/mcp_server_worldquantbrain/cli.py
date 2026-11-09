import argparse
import asyncio
import getpass
import sys

from pydantic import SecretStr, ValidationError

from mcp_server_worldquantbrain import __version__
from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.client.session import write_private
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import BrainError
from mcp_server_worldquantbrain.logging_setup import configure_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-server-worldquantbrain", description="MCP server for WorldQuant BRAIN."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    auth = commands.add_parser("auth", help="log in and store the session")
    auth.add_argument("--force", action="store_true", help="ignore the saved session")

    commands.add_parser("status", help="show the session state")
    commands.add_parser("tools", help="list the tools this configuration exposes")
    return parser


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    from mcp_server_worldquantbrain.server import create_server

    server = create_server(settings)
    if args.transport == "stdio":
        server.run()
        return 0
    import uvicorn

    from mcp_server_worldquantbrain.http_app import build_http_app

    app = build_http_app(server, settings, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


def _prompt_credentials(settings: Settings) -> None:
    print("No BRAIN credentials found.", file=sys.stderr)
    settings.email = input("Email: ").strip()
    settings.password = SecretStr(getpass.getpass("Password: "))


def _store_credentials(settings: Settings) -> None:
    assert settings.email and settings.password
    path = settings.resolved_credentials_file
    write_private(path, [settings.email, settings.password.get_secret_value()])
    print(f"Saved credentials to {path}", file=sys.stderr)


async def _auth(args: argparse.Namespace, settings: Settings) -> int:
    prompted = settings.credentials() is None
    if prompted:
        _prompt_credentials(settings)
    async with BrainClient(settings) as client:
        status = await client.authenticate(force=args.force or prompted)
        if prompted and status.state in ("authenticated", "biometric_required"):
            _store_credentials(settings)
        for _ in range(5):
            if status.state != "biometric_required":
                break
            print(f"\nFinish the biometric check in a browser:\n\n  {status.biometric_url}\n")
            input("Press Enter when done...")
            status = await client.complete_authentication()
        print(status.model_dump_json(indent=2))
        return 0 if status.state == "authenticated" else 1


async def _status(settings: Settings) -> int:
    async with BrainClient(settings) as client:
        status = await client.auth_status()
    print(status.model_dump_json(indent=2))
    return 0 if status.state == "authenticated" else 1


def _tools(settings: Settings) -> int:
    from mcp_server_worldquantbrain.ops import enabled_operations

    operations = enabled_operations(settings)
    width = max(len(op.name) for op in operations)
    for op in operations:
        print(f"{op.name:<{width}}  {op.title}")
    if not settings.allow_submit:
        print("\nsubmit_alpha is disabled. Set WQB_ALLOW_SUBMIT=true to enable it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        # Only names and reasons: the input values could be secrets.
        for error in exc.errors():
            name = "_".join(str(part) for part in error["loc"]).upper()
            print(f"Invalid WQB_{name}: {error['msg']}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    if args.command is None:
        args = _parser().parse_args(["serve"])
    try:
        if args.command == "serve":
            return _serve(args, settings)
        if args.command == "auth":
            return asyncio.run(_auth(args, settings))
        if args.command == "status":
            return asyncio.run(_status(settings))
        return _tools(settings)
    except BrainError as exc:
        print(exc.for_agent(), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
