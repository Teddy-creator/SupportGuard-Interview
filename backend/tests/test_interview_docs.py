from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_current_interview_documentation_matches_repository_facts() -> None:
    module = runpy.run_path(str(ROOT / "scripts/validate_interview_docs.py"))
    result = module["validate"]()

    assert result == {
        "result": "pass",
        "v20_activation": "phase7_human_acceptance",
        "current_authority_document_count": 8,
        "current_database_head": "i204_action_terminal_order",
        "v20_phase5_candidate_sha": "70717d8f19a9cbe3d8ead99db228c93f1577acc4",
        "v20_phase5_tree_sha": "853acfafd9782e2ce2d984cdd75da959718045a8",
        "v20_phase5_receipt_sha256": (
            "bbbc1d13156604b6bef8b36ccbeacd5bcdb2050c045d391ffeb26a8f15d55d8a"
        ),
        "v20_phase5_hosted_receipt_sha256": (
            "137939f829d4dff07c2454f48514d2b1d26135af633bb2617aa70f8945d9a886"
        ),
        "v20_phase5_hosted_disposition": "external_zero_step_blocker",
        "phase6_archive_manifest_sha256": (
            "7a62d7c3141d8a6c1bfc6460393d0329b285061dbcb2f229a2ecbaa6d7645f7f"
        ),
        "phase6_archive_file_count": 2197,
        "phase6_archive_source_commit": "328bc8606fdfbe50c9f3530646e72c1c21269c12",
        "phase6_test_disposition_status": "phase6_completed",
        "v20_phase6_candidate_sha": "30254587585fa2169cab071a926c501e06dac9a6",
        "v20_phase6_tree_sha": "199ca61783c5857cc95f83a468f1b80a5a313d81",
        "v20_phase6_receipt_sha256": (
            "e73b22d8888ace2838e135eaa5ce28d180c7dba5e476e932eb0e57e0c219d1d9"
        ),
        "v20_phase6_hosted_receipt_sha256": (
            "6bdb72e7b60ca994b561df1b88c7738acffedffe0d15f740b8a5902c07e1a41e"
        ),
        "v20_phase6_hosted_disposition": "external_zero_step_blocker",
        "phase7_failed_candidate_sha": "b132c395c2edf2d7d72477dc9051bffc3d7f4024",
        "phase7_failed_tree_sha": "78ed357459173ebb5354f24396fb42e96a22a98d",
        "phase7_failed_p16_receipt_sha256": (
            "68cf3f1d4c9bb8ade2fdca5b7b5d404cef3dc5822d751e34fbc416d245ec6bfa"
        ),
        "phase7_failed_p16_result": "11/16",
        "phase7_replacement_candidate_sha": ("7527c0acca079f57549538e49135a91ef87b9389"),
        "phase7_replacement_tree_sha": "b9d96a0dd984cf8874a00f8f00172ac6f34db4be",
        "phase7_replacement_p16_receipt_sha256": (
            "450a121f1bd77b8dd0beb9cb09a116ad0ba1993aee48f31917ce79f5f7f68e58"
        ),
        "phase7_replacement_p16_result": "13/16",
        "phase7_replacement_hosted_receipt_sha256": (
            "090e253cc4e2eb86167e240dc07a50bd18ad00d5aa6ce66562cfd95d72357eb0"
        ),
        "phase7_replacement_validation_receipt_sha256": (
            "f470c557f61d17b6abf3866f2d56111b9a2c33e5d26978f8b03fbc9c144c6150"
        ),
        "phase7_replacement_authorization_consumed": True,
        "phase7_confirmation_gate": False,
        "phase7_subsequent_clean_candidate_deepseek_authorized": True,
        "phase7_final_candidate_sha": "4466290963993e0b7662d75b571e4b15e4e97627",
        "phase7_final_tree_sha": "f4d021c13eac823d807cf3d120a99a610df9bb7b",
        "phase7_final_p16_receipt_sha256": (
            "21186631e6525743f1d1a617fe0e181500c9d2e1841531a355be500aa0ad45b5"
        ),
        "phase7_final_p16_result": "16/16",
        "phase7_final_hosted_receipt_sha256": (
            "1c82310915018a88f29762adba797ed0c97ed833208c11451d31e413de42c6b3"
        ),
        "phase7_final_validation_receipt_sha256": (
            "5dc7be8398169fb65dc265faec5a33e19caf20acd03f5df950c238c511b519f0"
        ),
        "phase7_machine_validation_complete": True,
        "human_acceptance_complete": False,
        "active_dataset": None,
        "protected_holdout": "not_accessed",
        "cross_encoder": "not_executed",
    }
