from __future__ import annotations


class RuntimeConflict(RuntimeError):
    """Stable fail-closed signal shared by runtime service boundaries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


__all__ = ["RuntimeConflict"]
