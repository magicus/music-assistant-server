"""Unit tests for pylyrion media parser helpers."""

from __future__ import annotations

from pylyrion import media_parsers


def test_split_lms_values_for_names_and_ids() -> None:
    """LMS scalar/list splitting should preserve normalized token order."""
    assert media_parsers.split_lms_values("a, b", split_mode="name") == ["a", "b"]
    assert media_parsers.split_lms_values("1,2,3", split_mode="id") == ["1", "2", "3"]
