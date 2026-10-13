"""Typed errors for camera commands.

Every command failure is mapped to one of these, so callers (service handlers) can tell a broken link
apart from a camera that refused the command or a setting that is not valid for this camera/preset.
"""

from __future__ import annotations


class GoProError(Exception):
    """Base class. ``context`` describes what the camera was doing (preset, encoding, transport)."""

    def __init__(self, message: str, context: str = "") -> None:
        self.message = message
        self.context = context
        super().__init__(f"{message} [{context}]" if context else message)


class TransportError(GoProError):
    """The link to the camera failed (timeout, disconnected, HTTP/BLE error)."""


class CommandTimeout(TransportError):
    """The command did not complete within its timeout."""


class NotConnected(TransportError):
    """No transport able to execute the command is connected."""


class CameraRejected(GoProError):
    """The camera answered but refused the command (e.g. busy, encoding, wrong mode)."""


class CameraBusy(CameraRejected):
    """The command requires an idle camera but it is busy or encoding."""


class InvalidSetting(GoProError):
    """Setting or option is unknown, not supported by this model, or not valid right now."""

    def __init__(self, message: str, context: str = "", supported_options: list[int] | None = None) -> None:
        super().__init__(message, context)
        self.supported_options = supported_options or []


class NotSupported(GoProError):
    """The feature is not available on this camera model or with the active transports."""


class DownloadCancelled(GoProError):
    """A download was stopped by its ``cancelled()`` callback (user request, or a capture starting).

    Distinct from ``asyncio.CancelledError``, which means the calling task itself is being cancelled.
    """
