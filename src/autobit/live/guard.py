"""No runtime configuration can authorize private venue activity."""


class LiveTradingLockedError(RuntimeError):
    """This release has no live execution authorization."""


def require_live_authorization() -> None:
    raise LiveTradingLockedError("Live trading is locked in this release")
