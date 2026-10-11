"""Unit tests for `demat.schema` (ADR 0013's added `pr_state`/`value_state`
fields must stay optional so an old payload — written before this change, or
produced by a stub skill — still validates.)
"""

from __future__ import annotations

from demat.schema import ExtractedSheet


def test_old_payload_without_state_fields_still_validates():
    payload = {
        "is_demat_sheet": True,
        "test_version": "DEMAT 2+",
        "test_form": "A",
        "child_name": "Max Mustermann",
        "child_name_confidence": "high",
        "klasse": "3c",
        "gender": "junge",
        "test_date_raw": "16.09.26",
        "test_date_iso": "2026-09-16",
        "test_date_confidence": "high",
        "subtests": [
            {
                "key": "ZE",
                "name": "Zahleneigenschaften",
                "rw": "12",
                "rw_max": "15",
                "pr": 63,
                "confidence": "high",
                # no pr_state
            }
        ],
        "overall": [
            {"key": "rw_total", "value": "88", "confidence": "high"}
            # no value_state
        ],
        "assessment_note": None,
        "illegible_notes": None,
        "overall_confidence": "high",
    }
    sheet = ExtractedSheet.model_validate(payload)
    assert sheet.subtests[0].pr_state is None
    assert sheet.overall[0].value_state is None


def test_new_payload_with_state_fields_validates():
    payload = {
        "is_demat_sheet": True,
        "test_version": "DEMAT 2+",
        "test_form": "A",
        "child_name": "Max Mustermann",
        "child_name_confidence": "high",
        "klasse": "3c",
        "gender": "junge",
        "test_date_raw": "16.09.26",
        "test_date_iso": "2026-09-16",
        "test_date_confidence": "high",
        "subtests": [
            {
                "key": "ZE",
                "name": "Zahleneigenschaften",
                "rw": "12",
                "rw_max": "15",
                "pr": None,
                "pr_state": "blank",
                "confidence": "high",
            }
        ],
        "overall": [
            {
                "key": "pr_total",
                "value": None,
                "value_state": "blank",
                "confidence": "high",
            }
        ],
        "assessment_note": None,
        "illegible_notes": None,
        "overall_confidence": "high",
    }
    sheet = ExtractedSheet.model_validate(payload)
    assert sheet.subtests[0].pr_state == "blank"
    assert sheet.overall[0].value_state == "blank"
