from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

SensitiveDataType = Literal[
    "email",
    "phone",
    "credit_card",
    "url_secret",
    "internal_url",
]


class SecurityAction(StrEnum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


class SecurityDecision(StrEnum):
    PASS = "pass"
    REDACT = "redact"
    BLOCK = "block"


class SecurityRequest(BaseModel):
    document: dict[str, Any]


class SecurityFinding(BaseModel):
    kind: SensitiveDataType
    action: SecurityAction
    path: str
    replacement: str | None = None


class SecurityResult(BaseModel):
    decision: SecurityDecision
    sanitized_document: dict[str, Any]
    sanitized_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: list[SecurityFinding] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision == SecurityDecision.BLOCK

    @property
    def has_sensitive_data(self) -> bool:
        return bool(self.findings)
