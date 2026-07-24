"""Connection to the local OpenD gateway. Data-only."""
import socket

from moomoo import OpenQuoteContext

HOST = "127.0.0.1"
PORT = 11111
CONNECT_TIMEOUT = 5.0    # seconds to prove OpenD is listening before we commit


class OpenDUnavailable(RuntimeError):
    """OpenD is not accepting connections on host:port."""


def _preflight(host: str, port: int, timeout: float) -> None:
    """Fail fast if nothing is listening.

    ⚠️ Why this exists (verified 2026-07-24): `OpenQuoteContext(host, port)`
    against a dead port **hangs forever** — it neither returns nor raises. The SDK
    retries the connection on a background thread with no overall deadline, so a
    caller's `try/except` is never reached.

    That silently defeats every "OpenD is down" handler we have. `collect_signals`
    is the worst case: its except-branch is what sets opend_ok=False and fires the
    phone alert, so with OpenD down it would hang at 20:15 Sunday, never alert, and
    leave a stuck process behind every week on a memory-tight box.

    A plain TCP connect is the cheap, dependency-free way to turn an unbounded hang
    into an immediate, catchable exception.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as e:
        raise OpenDUnavailable(
            f"OpenD not reachable at {host}:{port} ({type(e).__name__}: {e}). "
            "Is opend.service running? (shared with moomoo-vol-desk)") from e


def quote_ctx(host: str = HOST, port: int = PORT,
              connect_timeout: float = CONNECT_TIMEOUT) -> OpenQuoteContext:
    """Open a market-data context against the running OpenD. Caller closes it.

    Raises OpenDUnavailable (fast) instead of hanging when OpenD is down.
    """
    _preflight(host, port, connect_timeout)
    return OpenQuoteContext(host=host, port=port)
