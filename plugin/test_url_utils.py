"""Tests for URL canonicalization helpers."""
import pytest
from url_utils import strip_brackets, canonicalize_url


def test_strip_brackets_removes_angle_brackets():
    assert strip_brackets("<https://example.com>") == "https://example.com"

def test_strip_brackets_passthrough_no_brackets():
    assert strip_brackets("https://example.com") == "https://example.com"

def test_strip_brackets_handles_whitespace():
    assert strip_brackets("  <https://x.com>  ") == "https://x.com"

def test_canonicalize_url_linkedin_no_change():
    url = "https://www.linkedin.com/jobs/view/4410142713"
    assert canonicalize_url(url) == url

def test_canonicalize_url_linkedin_strips_query():
    url = "https://www.linkedin.com/jobs/view/4410142713?trk=public&refId=abc"
    expected = "https://www.linkedin.com/jobs/view/4410142713"
    assert canonicalize_url(url) == expected

def test_canonicalize_url_indeed_strips_from():
    url = "https://www.indeed.com/viewjob?jk=abc123&from=serp"
    expected = "https://www.indeed.com/viewjob?jk=abc123"
    assert canonicalize_url(url) == expected

def test_canonicalize_url_indeed_preserves_jk():
    url = "https://www.indeed.com/viewjob?jk=abc123"
    assert canonicalize_url(url) == url

def test_canonicalize_url_non_linkedin_indeed_passthrough():
    url = "https://glassdoor.com/job/foo?bar=baz"
    assert canonicalize_url(url) == url
