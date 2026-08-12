from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

from supportguard.services.business import _usage_bucket_complete

ROOT = Path(__file__).resolve().parents[2]


def _fixture_module() -> ModuleType:
    path = ROOT / "scripts" / "identity_bound_e2e_fixture.py"
    spec = importlib.util.spec_from_file_location("identity_bound_e2e_fixture_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("identity_bound_e2e_fixture_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_usage_fixture_remains_complete_across_one_minute_rollover() -> None:
    module = _fixture_module()
    first_window_end = datetime(2026, 7, 29, 13, 12, tzinfo=UTC)
    buckets = module.boundary_safe_usage_buckets(
        suffix="boundary",
        tenant_id="tenant_boundary",
        customer_id="customer_boundary",
        window_end=first_window_end,
    )

    for window_end in (first_window_end, first_window_end + timedelta(minutes=1)):
        window_start = window_end - timedelta(minutes=1)
        selected = [
            bucket
            for bucket in buckets
            if bucket.bucket_end > window_start and bucket.bucket_end <= window_end
        ]
        assert len(selected) == 1
        assert _usage_bucket_complete(
            [(bucket.bucket_start, bucket.bucket_end) for bucket in selected],
            window_start=window_start,
            window_end=window_end,
            expected_count=1,
        )
