from doc2agent.security import SecurityAction, SecurityGateway


def test_allows_public_http_url():
    gateway = SecurityGateway()

    document = {
        "text": "Docs: https://docs.python.org/3/library/urllib.parse.html"
    }

    result = gateway.process(document)

    assert result.blocked is False
    assert result.findings == []
    assert result.sanitized_document == document


def test_redacts_sensitive_query_parameter():
    gateway = SecurityGateway()

    document = {
        "text": "Open https://api.example.com/user?id=42&token=abcdef"
    }

    result = gateway.process(document)

    assert result.blocked is False
    assert (
        result.sanitized_document["text"]
        == "Open https://api.example.com/user?id=42&token=%5BREDACTED%5D"
    )

    assert len(result.findings) == 1
    assert result.findings[0].kind == "url_secret"
    assert result.findings[0].action == SecurityAction.REDACT


def test_redacts_url_credentials():
    gateway = SecurityGateway()

    document = {
        "text": "Open https://user:password@example.com/private"
    }

    result = gateway.process(document)

    assert result.blocked is False
    assert (
        result.sanitized_document["text"]
        == "Open https://example.com/private"
    )
    assert result.findings[0].kind == "url_secret"


def test_blocks_localhost_url():
    gateway = SecurityGateway()

    document = {
        "text": "Internal: http://localhost:8080/admin"
    }

    result = gateway.process(document)

    assert result.blocked is True
    assert (
        result.sanitized_document["text"]
        == "Internal: [BLOCKED_INTERNAL_URL]"
    )
    assert result.findings[0].kind == "internal_url"
    assert result.findings[0].action == SecurityAction.BLOCK


def test_blocks_private_ipv4_url():
    gateway = SecurityGateway()

    document = {
        "text": "Internal: http://192.168.1.15/admin"
    }

    result = gateway.process(document)

    assert result.blocked is True
    assert (
        result.sanitized_document["text"]
        == "Internal: [BLOCKED_INTERNAL_URL]"
    )


def test_blocks_internal_hostname():
    gateway = SecurityGateway()

    document = {
        "text": "Jira: https://jira.internal.company.local/browse/ABC-1"
    }

    result = gateway.process(document)

    assert result.blocked is True
    assert (
        result.sanitized_document["text"]
        == "Jira: [BLOCKED_INTERNAL_URL]"
    )


def test_sanitizes_markdown_link():
    gateway = SecurityGateway()

    document = {
        "text": (
            "[Download]"
            "(https://files.example.com/report.pdf?access_token=secret123)"
        )
    }

    result = gateway.process(document)

    assert result.blocked is False
    assert result.sanitized_document["text"] == (
        "[Download]"
        "(https://files.example.com/report.pdf?access_token=%5BREDACTED%5D)"
    )


def test_keeps_url_fragment():
    gateway = SecurityGateway()

    document = {
        "text": (
            "https://example.com/page?"
            "api_key=secret#section-2"
        )
    }

    result = gateway.process(document)

    assert result.sanitized_document["text"] == (
        "https://example.com/page?"
        "api_key=%5BREDACTED%5D#section-2"
    )


def test_preserves_trailing_punctuation():
    gateway = SecurityGateway()

    document = {
        "text": "See https://example.com/docs."
    }

    result = gateway.process(document)

    assert result.sanitized_document == document
