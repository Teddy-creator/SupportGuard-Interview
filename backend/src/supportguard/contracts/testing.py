"""Non-serializable capability for deterministic in-process test fixtures."""

from __future__ import annotations

from dataclasses import dataclass

_TEST_CAPABILITY_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class TestRuntimeCapability:
    _authority: object

    def __post_init__(self) -> None:
        if self._authority is not _TEST_CAPABILITY_AUTHORITY:
            raise RuntimeError("test runtime capability was not issued by the test authority")


def issue_test_runtime_capability(*, testing: bool) -> TestRuntimeCapability:
    if not testing:
        raise RuntimeError(
            "test runtime capability is unavailable outside an injected test runtime"
        )
    return TestRuntimeCapability(_TEST_CAPABILITY_AUTHORITY)
