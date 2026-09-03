from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from autobit.cli import build_parser


LEGACY_SOURCE_PATHS = (
    "main.py",
    "market_mode.py",
    "strategy_loader.py",
    "trade.py",
    "upbit_api.py",
    "config.py",
    "logutils.py",
    "analyze_log.py",
    "telegram_alert.py",
    "strategies/bull.py",
    "strategies/defensive.py",
    "strategies/sideways.py",
)
LEGACY_CACHE_PATHS = (
    "__pycache__/config.cpython-313.pyc",
    "__pycache__/logutils.cpython-313.pyc",
    "__pycache__/market_mode.cpython-313.pyc",
    "__pycache__/strategy.cpython-313.pyc",
    "__pycache__/strategy_loader.cpython-313.pyc",
    "__pycache__/telegram_alert.cpython-313.pyc",
    "__pycache__/trade.cpython-313.pyc",
    "__pycache__/upbit_api.cpython-313.pyc",
    "strategies/__pycache__/__init__.cpython-313.pyc",
    "strategies/__pycache__/bull.cpython-313.pyc",
    "strategies/__pycache__/defensive.cpython-313.pyc",
    "strategies/__pycache__/sideways.cpython-313.pyc",
)
EXPECTED_COMMANDS = (
    "data-download",
    "data-quality",
    "backtest",
    "walk-forward",
    "paper-once",
    "paper-run",
    "paper-status",
)
_SCANNED_SUFFIXES = frozenset({".py", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml"})
_EXCLUDED_TOP_LEVEL = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".superpowers",
        ".venv",
        ".worktrees",
        "data",
        "docs",
        "reports",
        "tests",
    }
)
_FORBIDDEN_SOURCE_TOKENS = (
    "buy" + "_market_order",
    "sell" + "_market_order",
    "create" + "_order",
    "place" + "_order",
    "create" + "_upbit",
    "cancel" + "_order",
    "get" + "_accounts",
    "get" + "_balance",
    "get" + "_balance_info",
    "upbit" + "_access_key",
    "upbit" + "_secret_key",
    "upbit" + "_api_key",
    "access" + "_key",
    "secret" + "_key",
    "author" + "ization",
    "bear" + "er",
    "j" + "wt",
    "py" + "upbit",
    "cc" + "xt",
    "wss" + "://",
    "/web" + "socket",
    "/v1/" + "orders",
    "/v1/" + "order",
    "/v1/" + "accounts",
    "/v1/" + "withdraws",
    "/v1/" + "deposits",
)
_LEGACY_IMPORT_TOKENS = tuple(
    form.format(module=module)
    for module in (
        "market_mode",
        "strategy_loader",
        "trade",
        "upbit_api",
        "logutils",
        "telegram_alert",
    )
    for form in ("from {module} import", "import {module}")
)


def _git_paths(*arguments: str) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        check=True,
        stdout=subprocess.PIPE,
    )
    return tuple(
        Path(item.decode("utf-8"))
        for item in completed.stdout.split(b"\0")
        if item
    )


def _is_repository_source(path: Path) -> bool:
    return (
        path.suffix.lower() in _SCANNED_SUFFIXES
        and path.parts
        and path.parts[0] not in _EXCLUDED_TOP_LEVEL
    )


def _repository_source_surfaces() -> tuple[tuple[str, str], ...]:
    surfaces: list[tuple[str, str]] = []
    for path in _git_paths("--cached"):
        if not _is_repository_source(path):
            continue
        staged = subprocess.run(
            ["git", "show", f":{path.as_posix()}"],
            check=True,
            stdout=subprocess.PIPE,
        )
        surfaces.append((f"index:{path.as_posix()}", staged.stdout.decode("utf-8")))
    for path in _git_paths("--cached", "--others", "--exclude-standard"):
        if path.is_file() and _is_repository_source(path):
            surfaces.append(
                (f"worktree:{path.as_posix()}", path.read_text(encoding="utf-8"))
            )
    return tuple(surfaces)


def test_exact_legacy_source_and_cache_paths_are_absent() -> None:
    tracked = frozenset(path.as_posix() for path in _git_paths("--cached"))
    present = tuple(
        path
        for path in (*LEGACY_SOURCE_PATHS, *LEGACY_CACHE_PATHS)
        if Path(path).exists() or path in tracked
    )

    assert present == ()


def test_no_root_python_entrypoint_remains() -> None:
    root_python = tuple(
        sorted(
            {
                path.name
                for path in (
                    *_git_paths("--cached"),
                    *Path(".").glob("*.py"),
                )
                if len(path.parts) == 1 and path.suffix.lower() == ".py"
            }
        )
    )

    assert root_python == ()


def test_no_tracked_python_bytecode_remains() -> None:
    tracked_bytecode = tuple(
        sorted(path.as_posix() for path in _git_paths("--cached", "*.pyc"))
    )

    assert tracked_bytecode == ()


def test_repository_source_has_no_private_upbit_or_live_order_surface() -> None:
    violations: list[str] = []
    for label, text in _repository_source_surfaces():
        source = text.lower()
        for token in (*_FORBIDDEN_SOURCE_TOKENS, *_LEGACY_IMPORT_TOKENS):
            if token.lower() in source:
                violations.append(f"{label}: {token}")

    assert violations == []


def test_network_api_urls_are_only_public_candles_and_optional_telegram() -> None:
    discovered: list[tuple[str, str]] = []
    for label, source in _repository_source_surfaces():
        for url in re.findall(r'https://[^\s"\']+', source):
            if url.startswith("https://api."):
                discovered.append((label, url))

    assert sorted(discovered) == [
        (
            "index:src/autobit/alerts/notifier.py",
            "https://api.telegram.org/bot{self._token}/sendMessage",
        ),
        (
            "index:src/autobit/data/upbit_public.py",
            "https://api.upbit.com/v1/candles/minutes/240",
        ),
        (
            "worktree:src/autobit/alerts/notifier.py",
            "https://api.telegram.org/bot{self._token}/sendMessage",
        ),
        (
            "worktree:src/autobit/data/upbit_public.py",
            "https://api.upbit.com/v1/candles/minutes/240",
        ),
    ]


def test_package_exposes_only_one_safe_console_script_and_dependency_set() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    dependencies = tuple(item.lower() for item in project["dependencies"])

    assert project["scripts"] == {"autobit": "autobit.cli:main"}
    assert not any(
        token in dependency
        for dependency in dependencies
        for token in ("py" + "upbit", "cc" + "xt", "j" + "wt")
    )


def test_cli_exposes_exactly_the_offline_public_commands_and_options() -> None:
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if action.dest == "command"
    )

    assert tuple(subparsers_action.choices) == EXPECTED_COMMANDS
    surface = [parser.format_help().lower()]
    for command, command_parser in subparsers_action.choices.items():
        surface.append(command.lower())
        surface.append(command_parser.format_help().lower())
        for action in command_parser._actions:
            surface.append(action.dest.lower())
            surface.extend(option.lower() for option in action.option_strings)
    rendered = "\n".join(surface)
    for token in (
        "live",
        "real-order",
        "account",
        "balance",
        "deposit",
        "withdraw",
        "credential",
        "access-key",
        "secret-key",
    ):
        assert token not in rendered
