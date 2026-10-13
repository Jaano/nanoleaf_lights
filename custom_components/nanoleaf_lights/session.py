"""Select a supported local protocol, trying LTPDUv3 before legacy CoAP."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .nl_ltpdu import AuthRejectedError

if TYPE_CHECKING:
    from .nl_ltpdu import LtpduSession
    from .nl_ltpdu_v3 import LtpduV3Session

    Session = LtpduSession | LtpduV3Session

_LOGGER = logging.getLogger(__name__)


async def _close_failed_session(session: Session) -> None:
    """Do not replace the connection error with a shutdown error."""
    try:
        await session.close()
    except Exception as exc:
        _LOGGER.debug("Failed to close unsuccessful session: %s", exc)


async def connect_session(
    ip: str,
    port: int,
    *,
    model: str | None = None,
    name: str | None = None,
    token: bytes | None = None,
    pin: str | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 -- passed to each backend operation
) -> Session:
    """Open/probe, authenticate, or pair; clean up before protocol fallback.

    Each backend's connect() owns cleanup until it returns its session. Once
    returned, this factory owns cleanup until authentication/pairing succeeds.
    Cancellation always propagates without starting another connection.
    """
    from .nl_ltpdu import LtpduSession
    from .nl_ltpdu_v3 import LtpduV3Session

    if pin is not None and token is not None:
        raise ValueError("Supply a pairing code or an access token, not both")

    failures: list[tuple[str, Exception]] = []
    for label, backend in (("LTPDUv3", LtpduV3Session), ("legacy CoAP", LtpduSession)):
        session = None
        try:
            options = {"model": model, "timeout": timeout}
            if backend is LtpduV3Session:
                options["name"] = name
            session = await backend.connect(ip, port, **options)
            if pin is not None:
                paired_token = await session.pair(pin, timeout=timeout)
                if backend is LtpduSession:
                    session.token = paired_token
            elif token is not None:
                await session.auth(token, timeout=timeout)
                if backend is LtpduSession:
                    session.token = token
            return session
        except asyncio.CancelledError:
            if session is not None:
                await _close_failed_session(session)
            raise
        except Exception as exc:
            if session is not None:
                await _close_failed_session(session)
            failures.append((label, exc))
            _LOGGER.debug("%s connection failed: %s", label, exc)

    message = "; ".join(f"{label}: {error}" for label, error in failures)
    rejection = next((error for _, error in failures if isinstance(error, AuthRejectedError)), None)
    if rejection is not None:
        raise AuthRejectedError(message) from rejection
    raise RuntimeError(message) from failures[-1][1]


def scene_handle_bytes(handle: bytes | int) -> bytes:
    """Keep opaque v3 IDs intact and convert legacy one-byte handles."""
    if isinstance(handle, bytes):
        return handle
    return handle.to_bytes(1 if handle <= 0xFF else 4, "big")


def scene_fallback_name(handle: bytes | int) -> str:
    """Stable fallback label for either scene identifier format."""
    return f"Scene 0x{scene_handle_bytes(handle).hex()}"
