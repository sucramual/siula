import json
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from siula.review.findings import (
    BLOCKING_SEVERITIES,
    SEVERITIES,
    Finding,
    Severity,
    parse_findings,
)


def finding(**changes: object) -> dict[str, object]:
    item: dict[str, object] = {
        "severity": "high",
        "file": "src/app/limits.py",
        "line": 42,
        "body": "A negative limit passes the check. Refuse values below 1.",
    }
    item.update(changes)
    return item


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).parent / "siula"
    return subprocess.run(
        [str(executable), "check-findings", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_severity_contract_is_pinned():
    # A downstream approval gate refuses approval on exactly these lowercase strings.
    assert SEVERITIES == ("blocker", "high", "medium", "low")
    assert BLOCKING_SEVERITIES == ("blocker", "high")
    assert get_args(Severity) == SEVERITIES


@pytest.mark.parametrize("severity", ["blocker", "high", "medium", "low"])
def test_each_severity_is_accepted(severity: str):
    findings = parse_findings(json.dumps([finding(severity=severity)]))
    assert [item.severity for item in findings] == [severity]


def test_empty_array_is_valid():
    assert parse_findings("[]") == []


def test_missing_line_is_none_and_text_is_kept_unchanged():
    item = finding(file=" src/a.py ", body="  Indented body.\n")
    del item["line"]
    [parsed] = parse_findings(json.dumps([item]))
    assert parsed == Finding(
        severity="high", file=" src/a.py ", line=None, body="  Indented body.\n"
    )


@pytest.mark.parametrize("severity", ["High", "HIGH", "critical", "P1", "", " high", None, 1])
def test_other_severity_values_are_refused(severity: object):
    with pytest.raises(ValidationError) as caught:
        parse_findings(json.dumps([finding(severity=severity)]))
    assert [error["loc"] for error in caught.value.errors()] == [(0, "severity")]


def test_missing_severity_is_refused():
    item = finding()
    del item["severity"]
    with pytest.raises(ValidationError) as caught:
        parse_findings(json.dumps([item]))
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [((0, "severity"), "missing")]


@pytest.mark.parametrize("key", ["file", "body"])
@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_blank_file_or_body_is_refused(key: str, value: str):
    with pytest.raises(ValidationError) as caught:
        parse_findings(json.dumps([finding(**{key: value})]))
    assert [error["loc"] for error in caught.value.errors()] == [(0, key)]


@pytest.mark.parametrize("line", [0, -1, True, False, "3", 1.5])
def test_bad_line_is_refused(line: object):
    with pytest.raises(ValidationError) as caught:
        parse_findings(json.dumps([finding(line=line)]))
    assert [error["loc"][:2] for error in caught.value.errors()] == [(0, "line")]


def test_unknown_key_is_refused():
    with pytest.raises(ValidationError) as caught:
        parse_findings(json.dumps([finding(confidence="high")]))
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
        ((0, "confidence"), "extra_forbidden")
    ]


@pytest.mark.parametrize("raw", ["", "[", "{}", "null", '"high"', json.dumps(finding())])
def test_input_that_is_not_a_json_array_is_refused(raw: str):
    with pytest.raises(ValidationError):
        parse_findings(raw)


def test_cli_prints_normalized_findings(tmp_path: Path):
    whole_file = finding(severity="low", file="README.md", body="Name the default.")
    del whole_file["line"]
    path = tmp_path / "findings.json"
    path.write_text(json.dumps([finding(), whole_file]), encoding="utf-8")

    result = run_cli(str(path))

    assert result.returncode == 0, result.stderr
    expected = [
        finding(),
        {"severity": "low", "file": "README.md", "line": None, "body": "Name the default."},
    ]
    assert result.stdout == json.dumps(expected, indent=2) + "\n"
    assert result.stderr == ""


def test_cli_accepts_empty_array(tmp_path: Path):
    path = tmp_path / "findings.json"
    path.write_text("[]\n", encoding="utf-8")
    result = run_cli(str(path))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_cli_refuses_invalid_severity_with_structured_error(tmp_path: Path):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps([finding(), finding(severity="P1")]), encoding="utf-8")

    result = run_cli(str(path))

    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["error"] == "invalid_findings"
    assert [detail["loc"] for detail in error["details"]] == [[1, "severity"]]
    assert "Traceback" not in result.stderr


def test_cli_refuses_unreadable_file_with_same_error_shape(tmp_path: Path):
    result = run_cli(str(tmp_path / "missing.json"))

    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["error"] == "invalid_findings"
    assert "missing.json" in error["details"][0]["message"]
