import ipaddress
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from doc2agent.artifacts import canonical_json_sha256

from .models import (
    SecurityAction,
    SecurityDecision,
    SecurityFinding,
    SecurityRequest,
    SecurityResult,
)


class SecurityGateway:
    IMMUTABLE_CONTRACT_PATHS = {
        "$.source_manifest.source_id",
        "$.structural_sha256",
    }

    EMAIL_PATTERN = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )

    PHONE_PATTERN = re.compile(
        r"(?<!\d)"
        r"(?:"
        r"\+972[-\s]?(?:5\d|[23489])[-\s]?\d{3}[-\s]?\d{4}"
        r"|"
        r"0(?:5\d|[23489])[-\s]?\d{3}[-\s]?\d{4}"
        r")"
        r"(?!\d)"
    )

    CREDIT_CARD_PATTERN = re.compile(
        r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"
    )

    URL_PATTERN = re.compile(
        r"https?://[^\s<>()\[\]{}\"']+",
        re.IGNORECASE,
    )

    EMAIL_REPLACEMENT = "[REDACTED_EMAIL]"
    PHONE_REPLACEMENT = "[REDACTED_PHONE]"
    CREDIT_CARD_REPLACEMENT = "[REDACTED_CREDIT_CARD]"
    URL_SECRET_REPLACEMENT = "[REDACTED]"

    SENSITIVE_QUERY_PARAMS = {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "key",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "auth",
        "authorization",
        "signature",
        "sig",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    }

    INTERNAL_HOST_SUFFIXES = (
        ".local",
        ".internal",
        ".intranet",
        ".corp",
        ".lan",
    )

    INTERNAL_HOSTNAMES = {
        "localhost",
    }

    def process(
        self,
        request: SecurityRequest | dict[str, Any],
    ) -> SecurityResult:
        if isinstance(request, dict):
            request = SecurityRequest(document=request)

        findings: list[SecurityFinding] = []

        sanitized_document = self._sanitize_value(
            value=request.document,
            path="$",
            findings=findings,
        )

        decision = self._resolve_decision(findings)

        return SecurityResult(
            decision=decision,
            sanitized_document=sanitized_document,
            sanitized_document_sha256=canonical_json_sha256(sanitized_document),
            findings=findings,
        )

    @staticmethod
    def _resolve_decision(
        findings: list[SecurityFinding],
    ) -> SecurityDecision:
        if any(
            finding.action == SecurityAction.BLOCK
            for finding in findings
        ):
            return SecurityDecision.BLOCK

        if any(
            finding.action == SecurityAction.REDACT
            for finding in findings
        ):
            return SecurityDecision.REDACT

        return SecurityDecision.PASS

    def _sanitize_value(
        self,
        value: Any,
        path: str,
        findings: list[SecurityFinding],
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: self._sanitize_value(
                    child,
                    f"{path}.{key}",
                    findings,
                )
                for key, child in value.items()
            }

        if isinstance(value, list):
            return [
                self._sanitize_value(
                    child,
                    f"{path}[{index}]",
                    findings,
                )
                for index, child in enumerate(value)
            ]

        if isinstance(value, str):
            if path in self.IMMUTABLE_CONTRACT_PATHS:
                return value
            return self._sanitize_text(
                text=value,
                path=path,
                findings=findings,
            )

        return value

    def _sanitize_text(
        self,
        text: str,
        path: str,
        findings: list[SecurityFinding],
    ) -> str:
        # URL must be processed before email, otherwise credentials inside
        # https://user@example.com could be partially modified first.
        text = self._sanitize_urls(
            text=text,
            path=path,
            findings=findings,
        )

        text = self._replace_pattern(
            text=text,
            pattern=self.EMAIL_PATTERN,
            kind="email",
            replacement=self.EMAIL_REPLACEMENT,
            path=path,
            findings=findings,
        )

        text = self._replace_credit_cards(
            text=text,
            path=path,
            findings=findings,
        )

        text = self._replace_pattern(
            text=text,
            pattern=self.PHONE_PATTERN,
            kind="phone",
            replacement=self.PHONE_REPLACEMENT,
            path=path,
            findings=findings,
        )

        return text

    def _sanitize_urls(
        self,
        *,
        text: str,
        path: str,
        findings: list[SecurityFinding],
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            raw_url = match.group(0)

            trailing = ""
            while raw_url and raw_url[-1] in ".,;:!?":
                trailing = raw_url[-1] + trailing
                raw_url = raw_url[:-1]

            sanitized = self._sanitize_url(
                raw_url,
                path=path,
                findings=findings,
            )
            return sanitized + trailing

        return self.URL_PATTERN.sub(replace, text)

    def _sanitize_url(
        self,
        url: str,
        *,
        path: str,
        findings: list[SecurityFinding],
    ) -> str:
        try:
            parts = urlsplit(url)
        except ValueError:
            return url

        hostname = parts.hostname

        if hostname and self._is_internal_host(hostname):
            findings.append(
                SecurityFinding(
                    kind="internal_url",
                    action=SecurityAction.BLOCK,
                    path=path,
                    replacement="[BLOCKED_INTERNAL_URL]",
                )
            )
            return "[BLOCKED_INTERNAL_URL]"

        netloc = parts.netloc

        if parts.username is not None or parts.password is not None:
            host_part = hostname or ""

            if parts.port is not None:
                host_part = f"{host_part}:{parts.port}"

            netloc = host_part

            findings.append(
                SecurityFinding(
                    kind="url_secret",
                    action=SecurityAction.REDACT,
                    path=path,
                    replacement="[REDACTED_URL_CREDENTIALS]",
                )
            )

        query_items = parse_qsl(
            parts.query,
            keep_blank_values=True,
        )

        sanitized_query_items: list[tuple[str, str]] = []

        for key, value in query_items:
            if key.lower() in self.SENSITIVE_QUERY_PARAMS:
                sanitized_query_items.append(
                    (key, self.URL_SECRET_REPLACEMENT)
                )

                findings.append(
                    SecurityFinding(
                        kind="url_secret",
                        action=SecurityAction.REDACT,
                        path=path,
                        replacement=self.URL_SECRET_REPLACEMENT,
                    )
                )
            else:
                sanitized_query_items.append((key, value))

        query = urlencode(
            sanitized_query_items,
            doseq=True,
        )

        return urlunsplit(
            (
                parts.scheme,
                netloc,
                parts.path,
                query,
                parts.fragment,
            )
        )

    def _is_internal_host(self, hostname: str) -> bool:
        normalized = hostname.lower().rstrip(".")

        if normalized in self.INTERNAL_HOSTNAMES:
            return True

        if normalized.endswith(self.INTERNAL_HOST_SUFFIXES):
            return True

        try:
            ip = ipaddress.ip_address(normalized)
        except ValueError:
            return False

        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
        )

    def _replace_pattern(
        self,
        *,
        text: str,
        pattern: re.Pattern[str],
        kind: str,
        replacement: str,
        path: str,
        findings: list[SecurityFinding],
    ) -> str:
        def replace(_: re.Match[str]) -> str:
            findings.append(
                SecurityFinding(
                    kind=kind,
                    action=SecurityAction.REDACT,
                    path=path,
                    replacement=replacement,
                )
            )
            return replacement

        return pattern.sub(replace, text)

    def _replace_credit_cards(
        self,
        *,
        text: str,
        path: str,
        findings: list[SecurityFinding],
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            candidate = match.group(0)
            digits = re.sub(r"\D", "", candidate)

            if not self._passes_luhn(digits):
                return candidate

            findings.append(
                SecurityFinding(
                    kind="credit_card",
                    action=SecurityAction.REDACT,
                    path=path,
                    replacement=self.CREDIT_CARD_REPLACEMENT,
                )
            )

            return self.CREDIT_CARD_REPLACEMENT

        return self.CREDIT_CARD_PATTERN.sub(replace, text)

    @staticmethod
    def _passes_luhn(value: str) -> bool:
        if not 13 <= len(value) <= 19:
            return False

        if not value.isdigit():
            return False

        total = 0

        for index, digit in enumerate(reversed(value)):
            number = int(digit)

            if index % 2 == 1:
                number *= 2

                if number > 9:
                    number -= 9

            total += number

        return total % 10 == 0
