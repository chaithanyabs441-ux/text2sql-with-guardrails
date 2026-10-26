class GuardrailException(Exception):
    """Base exception for all guardrail violations."""
    def __init__(self, message: str, layer: str):
        self.message = message
        self.layer = layer
        super().__init__(f"[{layer}] {message}")

class UnsafeQueryError(GuardrailException):
    """Raised when a query violates a security policy."""
    pass