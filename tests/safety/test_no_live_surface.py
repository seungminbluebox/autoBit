from __future__ import annotations

import ast
import base64
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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
_UNKNOWN = object()
_DYNAMIC_FIELD = "{dynamic}"
_PUBLIC_CANDLE_URL = "https://api.upbit.com/v1/candles/minutes/240"
_TELEGRAM_URL_TEMPLATE = "https://api.telegram.org/bot{dynamic}/sendMessage"
_ALLOWED_NETWORK_CONTRACTS = frozenset(
    {("GET", _PUBLIC_CANDLE_URL), ("POST", _TELEGRAM_URL_TEMPLATE)}
)
_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "base64",
        "binascii",
        "ccxt",
        "importlib",
        "logutils",
        "market_mode",
        "pyupbit",
        "strategy_loader",
        "subprocess",
        "telegram_alert",
        "trade",
        "upbit_api",
    }
)
_DYNAMIC_CALLS = frozenset({"__import__", "compile", "eval", "exec"})
_BASE64_DECODERS = frozenset(
    {"a2b_base64", "b64decode", "decodebytes", "standard_b64decode", "urlsafe_b64decode"}
)
_HTTP_METHOD_NAMES = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put"}
)
_NETWORK_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "asyncio",
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
    }
)
_DATA_ADAPTER_PATH = "src/autobit/data/upbit_public.py"
_NOTIFIER_ADAPTER_PATH = "src/autobit/alerts/notifier.py"
_HTTPX_WIRING_PATH = "src/autobit/cli.py"
_ALLOWED_HTTPX_IMPORT_PATHS = frozenset(
    {_DATA_ADAPTER_PATH, _NOTIFIER_ADAPTER_PATH, _HTTPX_WIRING_PATH}
)
_UNAMBIGUOUS_HTTP_SINK_METHODS = frozenset(
    {
        "delete",
        "head",
        "options",
        "patch",
        "post",
        "put",
        "request",
        "stream",
        "urlopen",
    }
)
_HTTP_SINK_METHODS = frozenset((*_HTTP_METHOD_NAMES, "request", "send", "stream"))
_HTTPX_NETWORK_ATTRIBUTES = frozenset(
    (*_HTTP_METHOD_NAMES, "request", "stream", "Client", "AsyncClient", "Request")
)
_PROCESS_EXECUTION_REFERENCES = frozenset({"os.popen", "os.system"})

# Exact reviewed ASTs, not a live-directory exemption. Changes to any of these
# modules fail closed until a new code/security review updates the fingerprint.
# Runtime tests separately prove the fixed guard precedes credentials, private
# request creation and journal mutation; fingerprints do not prove safety alone.
_AUDITED_LIVE_AST = {
    'src/autobit/live/__init__.py': '5b059888223aaecda805e67e81a4cb26bd1c975b827d76624dbcf6f74a71c5e4',
    'src/autobit/live/guard.py': '5fc5efbe8896d18e2cbafee8f45cf517096591af38d708058806c18a6fbab238',
    'src/autobit/live/client.py': '4952a1046fa97a9366cb0aa610237ddf8f011e6361634d7c1e3e0537369c7122',
    'src/autobit/live/journal.py': '4528fb1d76b6c4e060306ac3c980e14f76ce10997a9823b086fd55a2c155e271',
    'src/autobit/live/service.py': 'f19753f176c4ec5dce056eac8e839b98b5b702b92233c07fec2d2b6be885c481',
}
_LIVE_API_URLS = tuple('https://api.upbit.com' + path for path in (
    '/v1/accounts', '/v1/order', '/v1/orders', '/v1/orders/chance',
))


def _audited_live_scan(label: str, source: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path = _surface_path(label)
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError:
        return (f'{label}: invalid audited live source',), ()
    fingerprint = hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
    if fingerprint != _AUDITED_LIVE_AST[path]:
        return (f'{label}: unaudited change to locked live boundary',), ()
    return (), _LIVE_API_URLS if path == 'src/autobit/live/client.py' else ()


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


def _surface_path(label: str) -> str:
    prefix, separator, path = label.partition(":")
    if separator and prefix in {"index", "worktree"}:
        return path.replace("\\", "/")
    return label.replace("\\", "/")


def _parent_nodes(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _httpx_import_form_violations(tree: ast.AST, path: str) -> tuple[str, ...]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.lower().split(".", 1)[0] != "httpx":
                    continue
                if item.name != "httpx" or path not in _ALLOWED_HTTPX_IMPORT_PATHS:
                    violations.append(f"unapproved httpx module import: {item.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.lower().split(".", 1)[0] == "httpx":
                violations.append(f"httpx from-import is forbidden: {module}")
    return tuple(violations)


def _has_approved_httpx_import(tree: ast.AST, path: str) -> bool:
    return path in _ALLOWED_HTTPX_IMPORT_PATHS and any(
        isinstance(node, ast.Import)
        and any(item.name == "httpx" for item in node.names)
        for node in ast.walk(tree)
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


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _resolve_alias(name: str, aliases: dict[str, str]) -> str:
    direct = aliases.get(name)
    if direct is not None:
        return direct
    head, separator, tail = name.partition(".")
    resolved_head = aliases.get(head)
    if resolved_head is None:
        return name
    return resolved_head + (f".{tail}" if separator else "")


def _assignment_targets(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = _dotted_name(node)
        return (name,) if name else ()
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(
            name
            for element in node.elts
            for name in _assignment_targets(element)
        )
    return ()


def _imports(
    tree: ast.AST,
) -> tuple[dict[str, str], tuple[str, ...]]:
    aliases: dict[str, str] = {}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".")[0]
                aliases[local] = item.name
                if item.name.lower().split(".")[0] in _FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"forbidden import: {item.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.lower().split(".")[0]
            if root in _FORBIDDEN_IMPORT_ROOTS:
                violations.append(f"forbidden import: {module}")
            for item in node.names:
                local = item.asname or item.name
                aliases[local] = f"{module}.{item.name}" if module else item.name
    return aliases, tuple(violations)


def _constant_value(
    node: ast.AST,
    constants: dict[str, object],
    aliases: dict[str, str],
) -> object:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id, _UNKNOWN)
    if isinstance(node, ast.Attribute):
        name = _dotted_name(node)
        return constants.get(name or "", _UNKNOWN)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_value(node.left, constants, aliases)
        right = _constant_value(node.right, constants, aliases)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, bytes) and isinstance(right, bytes):
            return left + right
        return _UNKNOWN
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue) and part.format_spec is None:
                value = _constant_value(part.value, constants, aliases)
                parts.append(_DYNAMIC_FIELD if value is _UNKNOWN else str(value))
            else:
                return _UNKNOWN
        return "".join(parts)
    if not isinstance(node, ast.Call):
        return _UNKNOWN

    function = _dotted_name(node.func) or ""
    resolved_function = _resolve_alias(function, aliases)
    if isinstance(node.func, ast.Attribute) and node.func.attr == "decode":
        raw = _constant_value(node.func.value, constants, aliases)
        encoding = "utf-8"
        if node.args:
            requested = _constant_value(node.args[0], constants, aliases)
            if not isinstance(requested, str):
                return _UNKNOWN
            encoding = requested
        if isinstance(raw, (bytes, bytearray)):
            try:
                return bytes(raw).decode(encoding)
            except (LookupError, UnicodeDecodeError):
                return _UNKNOWN
    if isinstance(node.func, ast.Attribute) and node.func.attr.lower() == "fromhex":
        owner = _dotted_name(node.func.value)
        if owner in {"bytes", "bytearray"} and len(node.args) == 1:
            value = _constant_value(node.args[0], constants, aliases)
            if isinstance(value, str):
                try:
                    return bytes.fromhex(value)
                except ValueError:
                    return _UNKNOWN
    if resolved_function.lower().split(".")[-1] in _BASE64_DECODERS and node.args:
        value = _constant_value(node.args[0], constants, aliases)
        if isinstance(value, str):
            value = value.encode("ascii", errors="strict")
        if isinstance(value, bytes):
            try:
                return base64.b64decode(value)
            except (ValueError, TypeError):
                return _UNKNOWN
    if resolved_function.lower().endswith("codecs.decode") and len(node.args) >= 2:
        value = _constant_value(node.args[0], constants, aliases)
        encoding = _constant_value(node.args[1], constants, aliases)
        if isinstance(value, str) and isinstance(encoding, str) and encoding.lower() == "hex":
            try:
                return bytes.fromhex(value)
            except ValueError:
                return _UNKNOWN
    return _UNKNOWN


def _constant_bindings(
    tree: ast.AST,
    aliases: dict[str, str],
) -> dict[str, object]:
    assignments: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _assignment_targets(target):
                    assignments.setdefault(name, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            for name in _assignment_targets(node.target):
                assignments.setdefault(name, []).append(node.value)

    constants: dict[str, object] = {}
    for _ in range(len(assignments) + 1):
        changed = False
        for name, values in assignments.items():
            resolved = tuple(_constant_value(value, constants, aliases) for value in values)
            if not resolved or any(value is _UNKNOWN for value in resolved):
                continue
            first = resolved[0]
            if any(type(value) is not type(first) or value != first for value in resolved[1:]):
                continue
            if name not in constants:
                constants[name] = first
                changed = True
        if not changed:
            break
    return constants


def _annotation_is_httpx_client(
    annotation: ast.AST | None,
    aliases: dict[str, str],
) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        name = _dotted_name(node)
        resolved = _resolve_alias(name or "", aliases)
        if resolved in {"httpx.Client", "httpx.AsyncClient"}:
            return True
    return False


def _network_symbols(
    tree: ast.AST,
    aliases: dict[str, str],
) -> tuple[set[str], dict[str, str], set[str], set[str]]:
    httpx_modules = {local for local, target in aliases.items() if target == "httpx"}
    client_constructors = {
        local
        for local, target in aliases.items()
        if target in {"httpx.Client", "httpx.AsyncClient"}
    }
    request_constructors = {
        local for local, target in aliases.items() if target == "httpx.Request"
    }
    network_functions = {
        local: target.rsplit(".", 1)[-1].upper()
        for local, target in aliases.items()
        if target.rsplit(".", 1)[0] == "httpx"
        and target.rsplit(".", 1)[-1].lower() in (*_HTTP_METHOD_NAMES, "request")
    }
    clients: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for argument in arguments:
                if _annotation_is_httpx_client(argument.annotation, aliases):
                    clients.add(argument.arg)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is None or not isinstance(item.context_expr, ast.Call):
                    continue
                constructor = _dotted_name(item.context_expr.func) or ""
                resolved = _resolve_alias(constructor, aliases)
                if resolved in {"httpx.Client", "httpx.AsyncClient"}:
                    clients.update(_assignment_targets(item.optional_vars))

    assignments = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    )
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            targets = (
                tuple(name for target in node.targets for name in _assignment_targets(target))
                if isinstance(node, ast.Assign)
                else _assignment_targets(node.target)
            )
            call_name = (
                _dotted_name(value.func) if isinstance(value, ast.Call) else None
            )
            resolved_call = _resolve_alias(call_name or "", aliases)
            value_name = _dotted_name(value)
            if (
                resolved_call in {"httpx.Client", "httpx.AsyncClient"}
                or call_name in client_constructors
                or value_name in clients
            ):
                before = len(clients)
                clients.update(targets)
                changed = changed or len(clients) != before
            if isinstance(value, ast.Attribute):
                base = _dotted_name(value.value) or ""
                method = value.attr.lower()
                if (
                    (base in httpx_modules or base in clients)
                    and method in (*_HTTP_METHOD_NAMES, "request")
                ):
                    for target in targets:
                        if target not in network_functions:
                            network_functions[target] = method.upper()
                            changed = True
        if not changed:
            break
    return clients, network_functions, httpx_modules, request_constructors


def _call_argument(call: ast.Call, position: int, keyword: str) -> ast.AST | None:
    if len(call.args) > position:
        return call.args[position]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def _network_contract(
    method_node: ast.AST | None,
    url_node: ast.AST | None,
    *,
    fixed_method: str | None,
    constants: dict[str, object],
    aliases: dict[str, str],
) -> tuple[str, str] | None:
    method_value: object = fixed_method or _UNKNOWN
    if fixed_method is None and method_node is not None:
        method_value = _constant_value(method_node, constants, aliases)
    url_value = (
        _constant_value(url_node, constants, aliases)
        if url_node is not None
        else _UNKNOWN
    )
    if not isinstance(method_value, str) or not isinstance(url_value, str):
        return None
    return method_value.upper(), url_value


def _network_import_roots(aliases: dict[str, str]) -> frozenset[str]:
    return frozenset(
        target.lower().split(".", 1)[0]
        for target in aliases.values()
        if target.lower().split(".", 1)[0] in _NETWORK_IMPORT_ROOTS
    )


def _is_httpx_client_receiver(
    node: ast.AST,
    clients: set[str],
    aliases: dict[str, str],
) -> bool:
    name = _dotted_name(node) or ""
    if name in clients:
        return True
    if not isinstance(node, ast.Call):
        return False
    constructor = _resolve_alias(_dotted_name(node.func) or "", aliases)
    return constructor in {"httpx.Client", "httpx.AsyncClient"}


def _client_method_reference_is_allowed(
    node: ast.Attribute,
    clients: set[str],
    aliases: dict[str, str],
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if node.attr.lower() not in _HTTP_SINK_METHODS:
        return True
    if not _is_httpx_client_receiver(node.value, clients, aliases):
        return True
    parent = parents.get(node)
    return isinstance(parent, ast.Call) and parent.func is node


def _httpx_primitive_reference_is_allowed(
    node: ast.Attribute,
    path: str,
    aliases: dict[str, str],
    parents: dict[ast.AST, ast.AST],
) -> bool:
    resolved = _resolve_alias(_dotted_name(node) or "", aliases)
    if not resolved.startswith("httpx."):
        return True
    primitive = resolved.removeprefix("httpx.")
    if primitive not in _HTTPX_NETWORK_ATTRIBUTES:
        return True
    if primitive in _HTTP_METHOD_NAMES or primitive in {"request", "stream"}:
        return False

    parent = parents.get(node)
    if isinstance(parent, ast.Call) and parent.func is node:
        return True
    return (
        primitive == "Client"
        and path in {_DATA_ADAPTER_PATH, _NOTIFIER_ADAPTER_PATH}
        and isinstance(parent, ast.arg)
        and parent.annotation is node
    )


def _is_exact_benign_http_named_call(
    path: str,
    call: ast.Call,
    constants: dict[str, object],
    aliases: dict[str, str],
) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.keywords:
        return False
    base = _dotted_name(call.func.value) or ""
    method = call.func.attr.lower()

    if path == _DATA_ADAPTER_PATH:
        return (
            base == "response.headers"
            and method == "get"
            and len(call.args) == 1
            and _constant_value(call.args[0], constants, aliases) == "Remaining-Req"
        )
    if path == _NOTIFIER_ADAPTER_PATH and method == "send" and len(call.args) == 1:
        argument = call.args[0]
        return (
            base == "self._adapter"
            and isinstance(argument, ast.Name)
            and argument.id == "event"
        ) or (base == "notifier" and isinstance(argument, ast.Dict))
    if path != _HTTPX_WIRING_PATH or method != "get":
        return False
    if base == "fills":
        return (
            len(call.args) == 1
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "order_id"
        )
    if base == "os.environ":
        return (
            len(call.args) == 2
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id in {"chat_name", "token_name"}
            and _constant_value(call.args[1], constants, aliases) == ""
        )
    return (
        base == "payload"
        and len(call.args) == 1
        and _constant_value(call.args[0], constants, aliases)
        in {"processed_sha256", "quality", "schema_version"}
    )


def _looks_like_url_argument(
    node: ast.AST | None,
    constants: dict[str, object],
    aliases: dict[str, str],
) -> bool:
    if node is None:
        return False
    value = _constant_value(node, constants, aliases)
    if isinstance(value, str):
        return value.lower().startswith(("http://", "https://", "ws://", "wss://"))
    names = {
        child.id.lower()
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }
    return any(
        marker in name
        for name in names
        for marker in ("endpoint", "host", "uri", "url")
    )


def _network_call_is_allowed(
    path: str,
    kind: str,
    contract: tuple[str, str] | None,
    call: ast.Call,
) -> bool:
    if kind == "client_constructor":
        return path == _HTTPX_WIRING_PATH and not call.args and not call.keywords
    if path == _DATA_ADAPTER_PATH:
        return kind in {"request_constructor", "client_send"} and contract == (
            "GET",
            _PUBLIC_CANDLE_URL,
        )
    if path == _NOTIFIER_ADAPTER_PATH:
        return kind == "client_verb" and contract == (
            "POST",
            _TELEGRAM_URL_TEMPLATE,
        )
    return False


def _is_api_destination(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(
        (
            "http://api.",
            "https://api.",
            "ws://api.",
            "wss://api.",
            "http://sg-api.",
            "https://sg-api.",
            "ws://sg-api.",
            "wss://sg-api.",
        )
    )


def _python_surface_scan(label: str, source: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as error:
        return (f"{label}: invalid Python syntax: {error.msg}",), ()

    path = _surface_path(label)
    parents = _parent_nodes(tree)
    aliases, import_violations = _imports(tree)
    has_approved_httpx_import = _has_approved_httpx_import(tree, path)
    constants = _constant_bindings(tree, aliases)
    clients, network_functions, httpx_modules, request_constructors = _network_symbols(
        tree, aliases
    )
    violations = [
        f"{label}: {item}"
        for item in (*import_violations, *_httpx_import_form_violations(tree, path))
    ]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, 'module', '') or ''
            targets = [module, *(item.name for item in node.names)]
            if any('live' in target.split('.') for target in targets):
                violations.append(f'{label}: non-live code cannot import live boundary')
    for target in sorted(set(aliases.values()) & _PROCESS_EXECUTION_REFERENCES):
        violations.append(
            f"{label}: process execution import is forbidden: {target}"
        )
    for root in sorted(_network_import_roots(aliases)):
        if root != "httpx" or path not in _ALLOWED_HTTPX_IMPORT_PATHS:
            violations.append(
                f"{label}: network import outside approved boundary: {root}"
            )
    atoms: list[str] = []
    api_urls: set[str] = set()
    request_contracts: dict[str, tuple[str, str] | None] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            atoms.append(node.id)
        elif isinstance(node, ast.Attribute):
            atoms.append(node.attr)
            resolved_reference = _resolve_alias(_dotted_name(node) or "", aliases)
            if not _httpx_primitive_reference_is_allowed(
                node, path, aliases, parents
            ):
                violations.append(
                    f"{label}: direct httpx network primitive reference: "
                    f"{resolved_reference}"
                )
            if not _client_method_reference_is_allowed(
                node, clients, aliases, parents
            ):
                violations.append(
                    f"{label}: captured httpx client method is forbidden: "
                    f"{node.attr}"
                )
            if resolved_reference in _PROCESS_EXECUTION_REFERENCES:
                violations.append(
                    f"{label}: process execution capability is forbidden: "
                    f"{resolved_reference}"
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            atoms.append(node.name)
        elif isinstance(node, ast.arg):
            atoms.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            atoms.append(node.value)

        value = _constant_value(node, constants, aliases)
        if isinstance(value, bytes):
            try:
                atoms.append(value.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        elif isinstance(value, str):
            atoms.append(value)

        if isinstance(node, ast.Call):
            function = _dotted_name(node.func) or ""
            resolved = _resolve_alias(function, aliases)
            tail = resolved.lower().split(".")[-1]
            if (
                function in _DYNAMIC_CALLS
                or resolved in _DYNAMIC_CALLS
                or tail == "__import__"
            ):
                violations.append(f"{label}: dynamic execution/import call: {function}")
            if resolved in _PROCESS_EXECUTION_REFERENCES:
                violations.append(
                    f"{label}: process execution call is forbidden: {resolved}"
                )
            if isinstance(node.func, ast.Attribute) and node.func.attr.lower() == "fromhex":
                violations.append(f"{label}: constant decoder is forbidden: {function}")
            if tail in _BASE64_DECODERS:
                violations.append(f"{label}: base64 decoder is forbidden: {function}")
            if resolved.lower().endswith("codecs.decode"):
                violations.append(f"{label}: codecs.decode is forbidden")
            if function == "getattr" and node.args:
                target = _dotted_name(node.args[0]) or ""
                if target in httpx_modules or target in clients:
                    violations.append(f"{label}: dynamic network method lookup is forbidden")

    for value in constants.values():
        if isinstance(value, bytes):
            try:
                atoms.append(value.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        elif isinstance(value, str):
            atoms.append(value)
            if _is_api_destination(value):
                api_urls.add(value)
                if value not in {_PUBLIC_CANDLE_URL, _TELEGRAM_URL_TEMPLATE}:
                    violations.append(f"{label}: unapproved API URL constant: {value}")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not isinstance(value, ast.Call):
            continue
        function = _dotted_name(value.func) or ""
        resolved = _resolve_alias(function, aliases)
        is_request = resolved == "httpx.Request" or function in request_constructors
        if not is_request:
            continue
        contract = _network_contract(
            _call_argument(value, 0, "method"),
            _call_argument(value, 1, "url"),
            fixed_method=None,
            constants=constants,
            aliases=aliases,
        )
        targets = (
            tuple(name for target in node.targets for name in _assignment_targets(target))
            if isinstance(node, ast.Assign)
            else _assignment_targets(node.target)
        )
        for target in targets:
            request_contracts[target] = contract

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = _dotted_name(node.func) or ""
        resolved = _resolve_alias(function, aliases)
        kind: str | None = None
        contract: tuple[str, str] | None | object = _UNKNOWN
        if resolved == "httpx.Client":
            kind = "client_constructor"
            contract = None
        elif resolved == "httpx.AsyncClient":
            kind = "async_client_constructor"
            contract = None
        elif resolved == "httpx.Request" or function in request_constructors:
            kind = "request_constructor"
            contract = _network_contract(
                _call_argument(node, 0, "method"),
                _call_argument(node, 1, "url"),
                fixed_method=None,
                constants=constants,
                aliases=aliases,
            )
        elif isinstance(node.func, ast.Attribute):
            base = _dotted_name(node.func.value) or ""
            method = node.func.attr.lower()
            receiver_is_client = _is_httpx_client_receiver(
                node.func.value, clients, aliases
            )
            if base in httpx_modules and method in _HTTP_METHOD_NAMES:
                kind = "module_verb"
                contract = _network_contract(
                    None,
                    _call_argument(node, 0, "url"),
                    fixed_method=method.upper(),
                    constants=constants,
                    aliases=aliases,
                )
            elif (base in httpx_modules or receiver_is_client) and method == "request":
                kind = "client_request" if receiver_is_client else "module_request"
                contract = _network_contract(
                    _call_argument(node, 0, "method"),
                    _call_argument(node, 1, "url"),
                    fixed_method=None,
                    constants=constants,
                    aliases=aliases,
                )
            elif receiver_is_client and method in _HTTP_METHOD_NAMES:
                kind = "client_verb"
                contract = _network_contract(
                    None,
                    _call_argument(node, 0, "url"),
                    fixed_method=method.upper(),
                    constants=constants,
                    aliases=aliases,
                )
            elif receiver_is_client and method == "send":
                kind = "client_send"
                request_node = _call_argument(node, 0, "request")
                request_name = _dotted_name(request_node) if request_node is not None else None
                contract = request_contracts.get(request_name or "")
            elif base in httpx_modules and method == "stream":
                kind = "module_stream"
                contract = _network_contract(
                    _call_argument(node, 0, "method"),
                    _call_argument(node, 1, "url"),
                    fixed_method=None,
                    constants=constants,
                    aliases=aliases,
                )
            elif has_approved_httpx_import and _is_exact_benign_http_named_call(
                path, node, constants, aliases
            ):
                pass
            elif has_approved_httpx_import and method in _HTTP_SINK_METHODS:
                kind = "unresolved_receiver"
                if method == "request":
                    method_node = _call_argument(node, 0, "method")
                    url_node = _call_argument(node, 1, "url")
                    fixed_method = None
                elif method == "stream":
                    method_node = _call_argument(node, 0, "method")
                    url_node = _call_argument(node, 1, "url")
                    fixed_method = None
                elif method == "send":
                    method_node = None
                    url_node = None
                    fixed_method = None
                else:
                    method_node = None
                    url_node = _call_argument(node, 0, "url")
                    fixed_method = method.upper()
                contract = _network_contract(
                    method_node,
                    url_node,
                    fixed_method=fixed_method,
                    constants=constants,
                    aliases=aliases,
                )
            elif method in _UNAMBIGUOUS_HTTP_SINK_METHODS:
                kind = "unresolved_receiver"
                method_node = (
                    _call_argument(node, 0, "method")
                    if method == "request"
                    else None
                )
                url_node = _call_argument(
                    node,
                    1 if method == "request" else 0,
                    "url",
                )
                contract = _network_contract(
                    method_node,
                    url_node,
                    fixed_method=None if method == "request" else method.upper(),
                    constants=constants,
                    aliases=aliases,
                )
            elif method == "send" and path == _DATA_ADAPTER_PATH:
                kind = "unresolved_receiver"
                contract = None
            elif method == "get" and (
                (path in {_DATA_ADAPTER_PATH, _NOTIFIER_ADAPTER_PATH} and base != "response.headers")
                or _looks_like_url_argument(
                    _call_argument(node, 0, "url"), constants, aliases
                )
            ):
                kind = "unresolved_receiver"
                contract = _network_contract(
                    None,
                    _call_argument(node, 0, "url"),
                    fixed_method="GET",
                    constants=constants,
                    aliases=aliases,
                )
        elif isinstance(node.func, ast.Name) and node.func.id in network_functions:
            kind = "function_alias"
            method = network_functions[node.func.id]
            if method == "REQUEST":
                contract = _network_contract(
                    _call_argument(node, 0, "method"),
                    _call_argument(node, 1, "url"),
                    fixed_method=None,
                    constants=constants,
                    aliases=aliases,
                )
            else:
                contract = _network_contract(
                    None,
                    _call_argument(node, 0, "url"),
                    fixed_method=method,
                    constants=constants,
                    aliases=aliases,
                )
        elif resolved == "httpx.stream":
            kind = "function_alias"
            contract = _network_contract(
                _call_argument(node, 0, "method"),
                _call_argument(node, 1, "url"),
                fixed_method=None,
                constants=constants,
                aliases=aliases,
            )

        if kind is None:
            continue
        if contract is _UNKNOWN:
            contract = None
        if contract is None:
            if not _network_call_is_allowed(path, kind, None, node):
                violations.append(
                    f"{label}: unresolved or unapproved network capability: {function}"
                )
            continue
        method, url = contract
        if _is_api_destination(url):
            api_urls.add(url)
        if (
            contract not in _ALLOWED_NETWORK_CONTRACTS
            or not _network_call_is_allowed(path, kind, contract, node)
        ):
            violations.append(f"{label}: unapproved network request: {method} {url}")

    for atom in atoms:
        lowered = atom.lower()
        if 'autobit.live' in lowered or lowered == 'live':
            violations.append(f'{label}: non-live code cannot reference live boundary')
        for token in (*_FORBIDDEN_SOURCE_TOKENS, *_LEGACY_IMPORT_TOKENS):
            if token.lower() in lowered:
                violations.append(f"{label}: forbidden token: {token}")

    return tuple(sorted(set(violations))), tuple(sorted(api_urls))


def _surface_scan(label: str, source: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if _surface_path(label) in _AUDITED_LIVE_AST:
        return _audited_live_scan(label, source)
    if label.lower().endswith(".py"):
        return _python_surface_scan(label, source)
    lowered = source.lower()
    violations = tuple(
        sorted(
            f"{label}: forbidden token: {token}"
            for token in (*_FORBIDDEN_SOURCE_TOKENS, *_LEGACY_IMPORT_TOKENS)
            if token.lower() in lowered
        )
    )
    api_urls = tuple(
        sorted(
            url
            for url in re.findall(r'https://[^\s"\']+', source)
            if _is_api_destination(url)
        )
    )
    return violations, api_urls


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


def test_only_root_generated_pytest_basetemp_is_not_a_repository_source_surface() -> None:
    root_probe = Path(".test-tmp/_safety_root_fixture/probe.py")
    nested_probe = Path("src/autobit/.test-tmp/_safety_nested_fixture/probe.py")
    assert not root_probe.exists()
    assert not nested_probe.exists()
    owned_parents = tuple(
        parent for parent in (root_probe.parent.parent, nested_probe.parent.parent)
        if not parent.exists()
    )
    root_probe.parent.mkdir(parents=True)
    nested_probe.parent.mkdir(parents=True)
    root_probe.write_text("import pyupbit\n", encoding="utf-8")
    nested_probe.write_text("import pyupbit\n", encoding="utf-8")
    try:
        surfaces = dict(_repository_source_surfaces())
        root_label = f"worktree:{root_probe.as_posix()}"
        nested_label = f"worktree:{nested_probe.as_posix()}"

        assert root_label not in surfaces
        assert nested_label in surfaces
        violations, _ = _surface_scan(nested_label, surfaces[nested_label])
        assert f"{nested_label}: forbidden import: pyupbit" in violations
    finally:
        root_probe.unlink()
        root_probe.parent.rmdir()
        nested_probe.unlink()
        nested_probe.parent.rmdir()
        for parent in owned_parents:
            parent.rmdir()


@pytest.mark.parametrize("nonempty", [False, True], ids=["empty-parent", "nonempty-parent"])
def test_generated_probe_cleanup_preserves_preexisting_parent(nonempty: bool) -> None:
    parent = Path("src/autobit/.test-tmp")
    owned_parent = not parent.exists()
    parent.mkdir(exist_ok=True)
    marker = parent / "_ownership_regression.txt"
    assert not marker.exists()
    if nonempty:
        marker.write_text("unrelated retained content", encoding="utf-8")
    try:
        test_only_root_generated_pytest_basetemp_is_not_a_repository_source_surface()
        assert parent.is_dir()
        if nonempty:
            assert marker.read_text(encoding="utf-8") == "unrelated retained content"
    finally:
        if marker.exists():
            marker.unlink()
        if owned_parent and parent.exists():
            parent.rmdir()


def test_worktree_surface_scan_still_includes_an_unignored_package_module() -> None:
    probe = Path("src/autobit/_safety_surface_probe.py")
    assert not probe.exists()
    probe.write_text("import pyupbit\n", encoding="utf-8")
    try:
        surfaces = dict(_repository_source_surfaces())
        label = f"worktree:{probe.as_posix()}"

        assert label in surfaces
        violations, _ = _surface_scan(label, surfaces[label])
        assert f"{label}: forbidden import: pyupbit" in violations
    finally:
        probe.unlink()


def test_repository_source_has_only_the_audited_locked_live_surface() -> None:
    violations: list[str] = []
    for label, source in _repository_source_surfaces():
        surface_violations, _ = _surface_scan(label, source)
        violations.extend(surface_violations)

    assert violations == []


def test_network_api_urls_are_public_or_exact_audited_locked_live_contracts() -> None:
    discovered: list[tuple[str, str]] = []
    for label, source in _repository_source_surfaces():
        _, urls = _surface_scan(label, source)
        discovered.extend((label, url) for url in urls)

    expected = [
        (
            "index:src/autobit/alerts/notifier.py",
            _TELEGRAM_URL_TEMPLATE,
        ),
        (
            "index:src/autobit/data/upbit_public.py",
            "https://api.upbit.com/v1/candles/minutes/240",
        ),
        (
            "worktree:src/autobit/alerts/notifier.py",
            _TELEGRAM_URL_TEMPLATE,
        ),
        (
            "worktree:src/autobit/data/upbit_public.py",
            "https://api.upbit.com/v1/candles/minutes/240",
        ),
    ]
    # During staged development the new boundary may exist only in worktree;
    # each actually present indexed/worktree client must contribute exact URLs.
    for label, _ in _repository_source_surfaces():
        if _surface_path(label) == 'src/autobit/live/client.py':
            expected.extend((label, url) for url in _LIVE_API_URLS)
    assert sorted(discovered) == sorted(expected)


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
    # The sole approved mention is a fixed safety message, not a live command
    # or option. Continue scanning every remaining help/action/option surface.
    assert parser.epilog == (
        "Live trading is locked; paper and backtest remain available. No activation switch is provided."
    )
    parser.epilog = None
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


@pytest.mark.parametrize(
    ("case", "label", "source"),
    [
        (
            "adjacent_literals_index_renamed",
            "index:src/autobit/renamed_adapter.py",
            'import httpx as h\nURL = "https://" "api.upbit.com" "/v1/" "orders"\nh.post(URL)\n',
        ),
        (
            "literal_plus_worktree_renamed",
            "worktree:src/autobit/renamed_adapter.py",
            'import httpx as h\nURL = "https://" + "api.upbit.com" + "/v1/" + "orders"\nh.post(URL)\n',
        ),
        (
            "bytes_fromhex",
            "index:src/autobit/hex_adapter.py",
            'import httpx as h\nURL = "https://" + "api.upbit.com" + bytes.fromhex("2f76312f6f7264657273").decode()\nh.post(URL)\n',
        ),
        (
            "bytearray_fromhex",
            "worktree:src/autobit/bytearray_adapter.py",
            'import httpx as h\nURL = bytearray.fromhex("68747470733a2f2f6170692e75706269742e636f6d2f76312f6f7264657273").decode()\nh.post(URL)\n',
        ),
        (
            "base64_alias",
            "index:src/autobit/base64_adapter.py",
            'import base64 as harmless\nimport httpx as h\nURL = harmless.b64decode("aHR0cHM6Ly9hcGkudXBiaXQuY29tL3YxL29yZGVycw==").decode()\nh.post(URL)\n',
        ),
        (
            "codecs_alias",
            "worktree:src/autobit/codecs_adapter.py",
            'import codecs as harmless\nimport httpx as h\nURL = harmless.decode("68747470733a2f2f6170692e75706269742e636f6d2f76312f6f7264657273", "hex").decode()\nh.post(URL)\n',
        ),
        (
            "codecs_alias_without_network_sink",
            "index:src/autobit/codecs_only.py",
            'import codecs as harmless\nharmless.decode("61", "hex")\n',
        ),
        (
            "dynamic_dunder_import",
            "index:src/autobit/dynamic_adapter.py",
            'h = __import__("http" "x")\nURL = "https://" "api.upbit.com" "/v1/" "orders"\nh.post(URL)\n',
        ),
        (
            "dynamic_importlib_alias",
            "worktree:src/autobit/dynamic_adapter.py",
            'import importlib as harmless\nh = harmless.import_module("http" + "x")\nURL = "https://" "api.upbit.com" "/v1/" "orders"\nh.post(URL)\n',
        ),
        (
            "forbidden_client_alias",
            "index:src/autobit/client_adapter.py",
            "import pyupbit as public_candles\n",
        ),
        (
            "forbidden_client_from_alias",
            "worktree:src/autobit/client_adapter.py",
            "from ccxt import upbit as public_candles\n",
        ),
        (
            "uppercase_destination",
            "index:src/autobit/uppercase_adapter.py",
            'import httpx as h\nURL = "HTTPS://" "API.UPBIT.COM" "/V1/" "ORDERS"\nh.post(URL)\n',
        ),
        (
            "network_function_alias",
            "worktree:src/autobit/function_alias.py",
            'from httpx import post as publish\nURL = "https://" "api.upbit.com" "/v1/" "orders"\npublish(URL)\n',
        ),
        (
            "network_client_alias",
            "index:src/autobit/client_alias.py",
            'import httpx as h\nclient = h.Client()\nURL = "https://" "api.upbit.com" "/v1/" "orders"\nclient.post(URL)\n',
        ),
        (
            "network_client_alias_dynamic_url",
            "worktree:src/autobit/client_alias_dynamic.py",
            'import httpx as h\nclient = h.Client()\ndef send(host, path):\n    client.post(host + path)\n',
        ),
        (
            "inline_sync_client_dynamic_url",
            "index:src/autobit/alerts/notifier.py",
            'import httpx as h\ndef send(url):\n    h.Client().post(url)\n',
        ),
        (
            "inline_async_client_dynamic_url",
            "worktree:src/autobit/data/upbit_public.py",
            'import httpx as h\nasync def send(url):\n    await h.AsyncClient().post(url)\n',
        ),
        (
            "factory_client_dynamic_url",
            "index:src/autobit/alerts/notifier.py",
            'import httpx as h\ndef factory():\n    return h.Client()\nclient = factory()\ndef send(url):\n    client.post(url)\n',
        ),
        (
            "httpx_stream_dynamic_url",
            "worktree:src/autobit/data/upbit_public.py",
            'import httpx as h\ndef read(url):\n    return h.stream("GET", url)\n',
        ),
        (
            "urllib_urlopen_dynamic_url",
            "index:src/autobit/urllib_adapter.py",
            'from urllib.request import urlopen as fetch\ndef read(url):\n    return fetch(url)\n',
        ),
        (
            "builtins_dunder_import_alias_dynamic_url",
            "worktree:src/autobit/alerts/notifier.py",
            'import builtins as harmless\nh = harmless.__import__("httpx")\ndef send(url):\n    h.post(url)\n',
        ),
        (
            "requests_dynamic_url",
            "index:src/autobit/requests_adapter.py",
            'import requests as r\ndef send(url):\n    r.post(url)\n',
        ),
        (
            "aiohttp_dynamic_url",
            "worktree:src/autobit/aiohttp_adapter.py",
            'import aiohttp as a\nasync def send(url):\n    await a.ClientSession().post(url)\n',
        ),
        (
            "http_client_dynamic_host",
            "index:src/autobit/http_client_adapter.py",
            'import http.client as h\ndef connect(host):\n    h.HTTPSConnection(host)\n',
        ),
        (
            "socket_dynamic_address",
            "worktree:src/autobit/socket_adapter.py",
            'import socket as s\ndef connect(address):\n    s.socket().connect(address)\n',
        ),
        (
            "websocket_dynamic_url",
            "index:src/autobit/websocket_adapter.py",
            'import websocket as ws\ndef connect(url):\n    ws.create_connection(url)\n',
        ),
        (
            "websockets_dynamic_url",
            "worktree:src/autobit/websockets_adapter.py",
            'import websockets as ws\nasync def connect(url):\n    await ws.connect(url)\n',
        ),
        (
            "request_then_send",
            "worktree:src/autobit/request_alias.py",
            'import httpx as h\nclient = h.Client()\nURL = "https://" "api.upbit.com" "/v1/" "orders"\nrequest = h.Request("POST", URL)\nclient.send(request)\n',
        ),
        (
            "request_alias_dynamic_contract",
            "index:src/autobit/request_alias_dynamic.py",
            'import httpx as h\nclient = h.Client()\ndef send(method, url):\n    request = h.Request(method, url)\n    client.send(request)\n',
        ),
        (
            "unresolved_dynamic_url",
            "index:src/autobit/dynamic_url.py",
            'import httpx as h\ndef send(endpoint):\n    h.post("https://api.upbit.com/v1/" + endpoint)\n',
        ),
        (
            "wrong_public_method",
            "worktree:src/autobit/wrong_method.py",
            'import httpx as h\nh.post("https://api.upbit.com/v1/candles/minutes/240")\n',
        ),
        (
            "wrong_telegram_method",
            "index:src/autobit/wrong_telegram.py",
            'import httpx as h\ndef send(token):\n    h.get(f"https://api.telegram.org/bot{token}/sendMessage")\n',
        ),
        (
            "eval_capability",
            "index:src/autobit/eval_adapter.py",
            'URL = eval("\'https://\' + host + path")\n',
        ),
        (
            "exec_capability",
            "worktree:src/autobit/exec_adapter.py",
            'exec("URL = endpoint")\n',
        ),
        (
            "compile_capability",
            "index:src/autobit/compile_adapter.py",
            'compile("URL = endpoint", "<dynamic>", "exec")\n',
        ),
    ],
)
def test_scanner_rejects_private_constant_and_dynamic_bypasses(
    case: str,
    label: str,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del case
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: ((label, source),),
    )

    with pytest.raises(AssertionError):
        test_repository_source_has_only_the_audited_locked_live_surface()


@pytest.mark.parametrize(
    "label",
    [
        "index:src/autobit/cli.py",
        "worktree:src/autobit/data/upbit_public.py",
        "index:src/autobit/alerts/notifier.py",
    ],
)
@pytest.mark.parametrize(
    ("case", "body"),
    [
        (
            "dictionary_subscript",
            'methods = {"send": httpx.post}\ndef go(target):\n    return methods["send"](target)\n',
        ),
        (
            "default_argument",
            "def go(target, send=httpx.post):\n    return send(target)\n",
        ),
        (
            "tuple_alias",
            "(send,) = (httpx.post,)\ndef go(target):\n    return send(target)\n",
        ),
    ],
)
def test_scanner_rejects_httpx_callable_capture_in_every_approved_context(
    case: str,
    body: str,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del case
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: ((label, "import httpx\n" + body),),
    )

    with pytest.raises(AssertionError):
        test_repository_source_has_only_the_audited_locked_live_surface()


@pytest.mark.parametrize(
    ("case", "source"),
    [
        (
            "inline_client_get",
            "import httpx\ndef go(target):\n    return httpx.Client().get(target)\n",
        ),
        (
            "inline_async_client_get",
            "import httpx\nasync def go(target):\n    return await httpx.AsyncClient().get(target)\n",
        ),
        (
            "factory_client_get",
            "import httpx\ndef factory():\n    return httpx.Client()\ndef go(target):\n    return factory().get(target)\n",
        ),
    ],
)
def test_scanner_rejects_chained_get_in_cli_wiring(
    case: str,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del case
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: (("index:src/autobit/cli.py", source),),
    )

    with pytest.raises(AssertionError):
        test_repository_source_has_only_the_audited_locked_live_surface()


@pytest.mark.parametrize(
    ("case", "source"),
    [
        (
            "inline_client_bound_post",
            'import httpx\nmethods = {"send": httpx.Client().post}\ndef go(target):\n    return methods["send"](target)\n',
        ),
        (
            "inline_async_client_bound_post",
            'import httpx\nmethods = {"send": httpx.AsyncClient().post}\ndef go(target):\n    return methods["send"](target)\n',
        ),
        (
            "assigned_client_bound_post",
            'import httpx\nclient = httpx.Client()\nmethods = {"send": client.post}\ndef go(target):\n    return methods["send"](target)\n',
        ),
        (
            "client_named_like_benign_fills",
            "import httpx\nfills = httpx.Client()\ndef go(order_id):\n    return fills.get(order_id)\n",
        ),
    ],
)
def test_scanner_rejects_captured_client_methods_and_benign_name_collision(
    case: str,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del case
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: (("index:src/autobit/cli.py", source),),
    )

    with pytest.raises(AssertionError):
        test_repository_source_has_only_the_audited_locked_live_surface()


def test_scanner_allows_real_cli_fills_dictionary_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        'import httpx\nfills = {"paper-order": "fill"}\n'
        "def lookup(order_id):\n    return fills.get(order_id)\n"
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: (("worktree:src/autobit/cli.py", source),),
    )

    test_repository_source_has_only_the_audited_locked_live_surface()


@pytest.mark.parametrize(
    ("case", "source"),
    [
        (
            "asyncio_open_connection",
            "import asyncio\nasync def go(host):\n    return await asyncio.open_connection(host, 443)\n",
        ),
        (
            "subprocess_run_curl",
            'import subprocess\ndef go(target):\n    return subprocess.run(["curl", target])\n',
        ),
        (
            "subprocess_popen_wget",
            'import subprocess\ndef go(target):\n    return subprocess.Popen(["wget", target])\n',
        ),
        (
            "subprocess_check_call_powershell",
            'from subprocess import check_call\ndef go(target):\n    return check_call(["powershell", "Invoke-WebRequest", target])\n',
        ),
        (
            "os_system_curl",
            'import os\ndef go(target):\n    return os.system("curl " + target)\n',
        ),
        (
            "os_popen_wget",
            'import os\ndef go(target):\n    return os.popen("wget " + target)\n',
        ),
    ],
)
def test_scanner_rejects_raw_network_and_process_escape_hatches(
    case: str,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del case
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: (("worktree:src/autobit/escape_hatch.py", source),),
    )

    with pytest.raises(AssertionError):
        test_repository_source_has_only_the_audited_locked_live_surface()


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "index:src/autobit/data/upbit_public.py",
            'import httpx\nURL = "https://api.upbit.com/v1/candles/minutes/240"\nclass Adapter:\n    def __init__(self, client: httpx.Client):\n        self._client = client\n    def send(self):\n        request = httpx.Request("GET", URL)\n        return self._client.send(request, auth=None, follow_redirects=False)\n',
        ),
        (
            "worktree:src/autobit/alerts/notifier.py",
            'import httpx\nclass Adapter:\n    def __init__(self, client: httpx.Client):\n        self._client = client\n    def send(self, token):\n        return self._client.post(f"https://api.telegram.org/bot{token}/sendMessage", auth=None)\n',
        ),
        (
            "index:src/autobit/cli.py",
            'import httpx\ndef build():\n    with httpx.Client() as client:\n        return client\n',
        ),
    ],
)
def test_scanner_allows_only_exact_network_controls(
    label: str,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys.modules[__name__],
        "_repository_source_surfaces",
        lambda: ((label, source),),
    )

    test_repository_source_has_only_the_audited_locked_live_surface()
