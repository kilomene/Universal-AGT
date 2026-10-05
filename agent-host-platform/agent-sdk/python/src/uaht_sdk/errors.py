"""Errors raised by the uaht_sdk client."""


class UahtError(Exception):
    """Raised for every control-plane failure.

    Mirrors the protocol error shape ``{"error": {"code": ..., "message": ...}}``.
    """

    def __init__(self, code: str, message: str, status=None, body=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.body = body
