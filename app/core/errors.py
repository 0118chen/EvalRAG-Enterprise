"""Shared errors for optional external services."""


class BackendUnavailableError(RuntimeError):
    """A transient connection/timeout failure eligible for local fallback."""
