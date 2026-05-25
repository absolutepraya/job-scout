"""Tests for config validators + ruamel.yaml round-trip."""
import pytest
from config import validate_value, ValidationError, EDITABLE_KEYS, DEFAULTS


def test_validate_int_within_range():
    assert validate_value("min_score", "40") == 40
    assert validate_value("min_score", 0) == 0
    assert validate_value("min_score", 100) == 100

def test_validate_int_out_of_range():
    with pytest.raises(ValidationError, match="0-100"):
        validate_value("min_score", "-1")
    with pytest.raises(ValidationError, match="0-100"):
        validate_value("min_score", "101")

def test_validate_int_non_numeric():
    with pytest.raises(ValidationError, match="integer"):
        validate_value("min_score", "abc")

def test_validate_hours_old():
    assert validate_value("hours_old", "168") == 168
    with pytest.raises(ValidationError):
        validate_value("hours_old", "0")
    with pytest.raises(ValidationError):
        validate_value("hours_old", "721")

def test_validate_workmode_blacklist_entry():
    assert validate_value("workmode_blacklist", "remote") == "remote"
    with pytest.raises(ValidationError, match="onsite, remote, abroad"):
        validate_value("workmode_blacklist", "hovercraft")

def test_validate_title_blacklist_entry():
    assert validate_value("title_must_not_match", "marketing") == "marketing"
    with pytest.raises(ValidationError, match="non-empty"):
        validate_value("title_must_not_match", "")
    long = "x" * 51
    with pytest.raises(ValidationError, match="50 chars"):
        validate_value("title_must_not_match", long)

def test_validate_query_entry():
    assert validate_value("queries", "AI Engineer Intern") == "AI Engineer Intern"
    long = "x" * 101
    with pytest.raises(ValidationError, match="100 chars"):
        validate_value("queries", long)

def test_validate_unknown_key():
    with pytest.raises(ValidationError, match="not editable"):
        validate_value("locations", "Jakarta")
