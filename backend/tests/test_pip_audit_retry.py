from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "supportguard_pip_audit_retry", ROOT / "scripts/pip_audit_retry.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pip_audit_retries_only_transient_transport_failure() -> None:
    module = _module()
    outcomes = iter(
        [
            subprocess.CompletedProcess([], 1, "", "SSLError: UNEXPECTED_EOF"),
            subprocess.CompletedProcess([], 0, "No known vulnerabilities found\n", ""),
        ]
    )
    sleeps: list[float] = []
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return next(outcomes)

    assert module.run_audit(runner=run, sleeper=sleeps.append) == 0
    assert calls == [[sys.executable, "-m", "pip_audit", "--skip-editable"]] * 2
    assert sleeps == [1.0]


def test_pip_audit_vulnerability_result_is_never_retried() -> None:
    module = _module()
    calls = 0

    def run(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess([], 1, "Found 1 known vulnerability\n", "")

    assert module.run_audit(runner=run, sleeper=lambda _seconds: None) == 1
    assert calls == 1


def test_pip_audit_transient_retry_budget_is_bounded() -> None:
    module = _module()
    calls = 0

    def run(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess([], 1, "", "503 Service Unavailable")

    assert module.run_audit(runner=run, sleeper=lambda _seconds: None) == 1
    assert calls == module.MAX_ATTEMPTS == 3
