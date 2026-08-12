"""Fail closed unless Playwright discovers the frozen current product suite."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "frontend/playwright.config.ts"


class E2EDiscoveryError(RuntimeError):
    pass


def _contract() -> tuple[str, int]:
    source = CONFIG.read_text(encoding="utf-8")
    file_match = re.search(r'CURRENT_E2E_FILE = "([^"]+)"', source)
    count_match = re.search(r"CURRENT_E2E_EXPECTED_TESTS = (\d+)", source)
    if file_match is None or count_match is None:
        raise E2EDiscoveryError("current_e2e_contract_missing")
    return file_match.group(1), int(count_match.group(1))


def validate() -> dict[str, object]:
    expected_file, expected_count = _contract()
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise E2EDiscoveryError("pnpm_not_found")
    completed = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
        [pnpm, "--dir", "frontend", "exec", "playwright", "test", "--list"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise E2EDiscoveryError("playwright_list_failed")
    total = re.search(r"Total:\s+(\d+)\s+tests?\s+in\s+(\d+)\s+files?", output)
    if total is None:
        raise E2EDiscoveryError("playwright_list_summary_missing")
    observed_count = int(total.group(1))
    observed_files = {
        match.group(1)
        for match in re.finditer(r"(?:e2e/)?([^:/\s]+\.spec\.ts):\d+", output)
    }
    if observed_count != expected_count:
        raise E2EDiscoveryError(
            f"current_e2e_count_mismatch:{observed_count}!={expected_count}"
        )
    if observed_files != {expected_file}:
        raise E2EDiscoveryError(
            f"current_e2e_file_mismatch:{sorted(observed_files)}"
        )
    return {
        "schema": "supportguard-e2e-discovery.v1",
        "expected_file": expected_file,
        "expected_tests": expected_count,
        "observed_files": sorted(observed_files),
        "observed_tests": observed_count,
        "historical_golden_excluded": "golden-scenarios.spec.ts" not in observed_files,
    }


def main() -> None:
    print(json.dumps(validate(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
