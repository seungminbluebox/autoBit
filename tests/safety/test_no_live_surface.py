from pathlib import Path

from autobit.cli import build_parser


FORBIDDEN_SOURCE_TOKENS = (
    "buy_market_order",
    "sell_market_order",
    "create_order",
    "cancel_order",
    "get_accounts",
    "UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY",
    "UPBIT_API_KEY",
    "/v1/orders",
    "/v1/order",
    "/v1/accounts",
    "/v1/withdraws",
    "/v1/deposits",
)
FORBIDDEN_HELP_TOKENS = (
    "live",
    "buy",
    "sell",
    "order",
    "account",
    "credential",
)


def test_source_contains_no_private_upbit_or_live_order_surface() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/autobit").rglob("*.py")
    )

    for token in FORBIDDEN_SOURCE_TOKENS:
        assert token not in source


def test_cli_exposes_exactly_the_offline_public_commands() -> None:
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if action.dest == "command"
    )

    assert tuple(subparsers_action.choices) == (
        "data-download",
        "data-quality",
        "backtest",
        "walk-forward",
    )
    help_text = parser.format_help().lower()
    for token in FORBIDDEN_HELP_TOKENS:
        assert token not in help_text
