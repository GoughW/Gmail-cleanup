class CleanupError(Exception):
    """Safe, content-free error suitable for the CLI."""


class Unsupported(CleanupError):
    """MIME or metadata that this version deliberately refuses to transform."""


class MessageNotFound(CleanupError):
    """Only an actual Gmail HTTP 404, never a network/authentication error."""
