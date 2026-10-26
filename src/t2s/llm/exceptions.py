"""Exceptions shared by provider adapters and generation code."""


class ProviderRefusalError(RuntimeError):
    """Raised when a model provider refuses a request instead of returning JSON."""
