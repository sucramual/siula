"""The findings contract: the JSON array that a read-only reviewer writes and a gate reads.

The severity strings are a contract. A downstream approval gate refuses approval when any
finding has a severity in BLOCKING_SEVERITIES, so this module accepts no other spelling.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SEVERITIES = ("blocker", "high", "medium", "low")
BLOCKING_SEVERITIES = ("blocker", "high")
Severity = Literal["blocker", "high", "medium", "low"]


class Finding(BaseModel):
    """One reviewer finding. Unknown keys and loose types are refused."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    severity: Severity
    file: str
    line: Annotated[int, Field(ge=1)] | None = None
    body: str

    @field_validator("file", "body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value


_FINDINGS = TypeAdapter(list[Finding])


def parse_findings(raw: str) -> list[Finding]:
    """Parse a JSON array of findings. Raise pydantic.ValidationError on any other input."""
    return _FINDINGS.validate_json(raw, strict=True)
