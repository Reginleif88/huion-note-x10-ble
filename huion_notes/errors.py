"""Shared exceptions for the note extractor."""
from __future__ import annotations


class TransportClosed(Exception):
    """The BLE transport closed (device disconnected / EOF)."""


class PinRequired(Exception):
    """Device requested a PIN but none was supplied."""


class AuthFailed(Exception):
    """The device rejected the auth handshake."""
