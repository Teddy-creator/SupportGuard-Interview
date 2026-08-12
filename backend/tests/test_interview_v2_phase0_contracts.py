from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _validator() -> ModuleType:
    path = ROOT / "scripts" / "validate_interview_v2_phase0.py"
    spec = importlib.util.spec_from_file_location("validate_interview_v2_phase0", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_interview_v2_phase0_inputs_are_frozen_and_self_consistent() -> None:
    result = _validator().validate()

    assert result["result"] == "pass"
    assert result["archive"] == {"files": 2617, "restore": "pass", "remote": "passed"}
    assert result["code_map"] == {"entries": 12, "owner_maps": 3}
    assert result["frozen_matrices"]["ie_p16"] == 16
    assert result["frozen_matrices"]["ie_f06"] == 6
    assert result["frozen_matrices"]["ie_j12"] == 12
    assert result["rag_dev30"]["cases"] == 30
    assert result["protected_evaluation_accessed"] is False
    assert result["provider_executed"] is False
