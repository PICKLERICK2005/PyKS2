"""Exception types for pyks2.

The K-S2 reports logical failures in the JSON body's ``errCode`` field while the
HTTP status stays 200 (see PROTOCOL.md, Law 1). These exceptions surface that
cleanly so callers can catch camera-level errors distinctly from transport
errors.
"""

from __future__ import annotations

from typing import Optional


class KS2Error(Exception):
    """Base class for all pyks2 errors."""


class KS2ConnectionError(KS2Error):
    """Transport-level failure: unreachable host, timeout, dropped socket.

    Commonly raised when the camera's WiFi drops — e.g. the SD card door was
    opened (which kills the connection), the camera powered down, or the client
    roamed off the camera's access point.
    """


class KS2APIError(KS2Error):
    """The camera returned a non-200 ``errCode`` in the response body.

    Attributes:
        err_code: The camera's ``errCode`` (e.g. 400, 412).
        err_msg:  The camera's ``errMsg`` (e.g. "Bad Request").
        endpoint: The path that produced the error, if known.
    """

    def __init__(self, err_code: int, err_msg: str = "",
                 endpoint: Optional[str] = None):
        self.err_code = err_code
        self.err_msg = err_msg
        self.endpoint = endpoint
        loc = f" [{endpoint}]" if endpoint else ""
        super().__init__(f"camera returned errCode {err_code} "
                         f"({err_msg or 'no message'}){loc}")


class KS2UnsupportedError(KS2APIError):
    """A known-unsupported operation was attempted.

    Raised for operations the hardware forbids over WiFi (see PROTOCOL.md §5/§6):
    writing ``focusMode`` or ``device`` params, deleting photos, and
    ``size=thumb``. These fail deterministically, so pyks2 can raise a clearer
    error than a bare 400/412. (Note: ``shoot/start``/``shoot/finish`` are NOT
    unsupported — they are the working Bulb-exposure controls; see
    ``K_S2_WiFi.bulb_start``/``bulb_finish``.)
    """


class KS2NotFoundError(KS2Error):
    """A requested resource (photo path, directory) does not exist."""
