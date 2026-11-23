"""The one error type Agora services raise; tools turn it into an envelope."""
from __future__ import annotations

ERROR_CODES: frozenset[str] = frozenset({
    "invalid",
    "not_found",
    "forbidden",
    "conflict",
    "too_large",
    "secret_detected",
    "rate_limited",
    "contact_required",
})


class AgoraError(Exception):
    """A refused Agora operation. ``code`` is one of :data:`ERROR_CODES`.

    ``message`` is safe to return to the caller: it never repeats a body, a
    token or a detected secret.
    """

    def __init__(self, code: str, message: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown Agora error code {code!r}")
        super().__init__(code, message)
        self.code = code
        self.message = message or code

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
