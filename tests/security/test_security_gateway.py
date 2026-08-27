from doc2agent.security import SecurityAction, SecurityGateway
from doc2agent.security.models import SecurityDecision

def test_allows_document_without_sensitive_data():
    gateway = SecurityGateway()

    document = {
        "title": "Test document",
        "blocks": [
            {
                "type": "paragraph",
                "text": "This is normal text.",
            }
        ],
    }

    result = gateway.process(document)

    assert result.decision == SecurityDecision.PASS
    assert result.has_sensitive_data is False
    assert result.blocked is False
    assert result.findings == []
    assert result.sanitized_document == document


def test_redacts_email():
    gateway = SecurityGateway()

    document = {
        "text": "Contact user@example.com for more information."
    }

    result = gateway.process(document)

    assert result.decision == SecurityDecision.REDACT
    assert result.has_sensitive_data is True
    assert result.blocked is False
    assert (
        result.sanitized_document["text"]
        == "Contact [REDACTED_EMAIL] for more information."
    )
    assert len(result.findings) == 1
    assert result.findings[0].kind == "email"
    assert result.findings[0].action == SecurityAction.REDACT
    assert result.findings[0].path == "$.text"


def test_redacts_israeli_phone_number():
    gateway = SecurityGateway()

    document = {
        "text": "Phone: 050-123-4567"
    }

    result = gateway.process(document)

    assert result.has_sensitive_data is True
    assert (
        result.sanitized_document["text"]
        == "Phone: [REDACTED_PHONE]"
    )
    assert result.findings[0].kind == "phone"


def test_redacts_credit_card():
    gateway = SecurityGateway()

    document = {
        "text": "Card: 4111 1111 1111 1111"
    }

    result = gateway.process(document)

    assert result.has_sensitive_data is True
    assert (
        result.sanitized_document["text"]
        == "Card: [REDACTED_CREDIT_CARD]"
    )
    assert result.findings[0].kind == "credit_card"


def test_does_not_redact_invalid_credit_card_number():
    gateway = SecurityGateway()

    document = {
        "text": "Reference: 1234 5678 9012 3456"
    }

    result = gateway.process(document)

    assert result.sanitized_document == document


def test_preserves_nested_document_structure():
    gateway = SecurityGateway()

    document = {
        "metadata": {
            "language": "he",
        },
        "blocks": [
            {
                "type": "paragraph",
                "text": "Email: test@example.com",
            },
            {
                "type": "table",
                "rows": [
                    ["Name", "Phone"],
                    ["David", "050-123-4567"],
                ],
            },
        ],
    }

    result = gateway.process(document)

    sanitized = result.sanitized_document

    assert sanitized["metadata"]["language"] == "he"
    assert sanitized["blocks"][0]["type"] == "paragraph"
    assert (
        sanitized["blocks"][0]["text"]
        == "Email: [REDACTED_EMAIL]"
    )
    assert sanitized["blocks"][1]["type"] == "table"
    assert sanitized["blocks"][1]["rows"][0] == [
        "Name",
        "Phone",
    ]
    assert sanitized["blocks"][1]["rows"][1] == [
        "David",
        "[REDACTED_PHONE]",
    ]


def test_does_not_modify_original_document():
    gateway = SecurityGateway()

    document = {
        "text": "Email: test@example.com"
    }

    gateway.process(document)

    assert document == {
        "text": "Email: test@example.com"
    }

    def test_blocks_document_with_internal_url():
        gateway = SecurityGateway()

        document = {
            "text": "Open http://localhost/admin"
        }

        result = gateway.process(document)

        assert result.decision == SecurityDecision.BLOCK
        assert result.blocked is True
        assert (
            result.sanitized_document["text"]
            == "Open [BLOCKED_INTERNAL_URL]"
        )
        assert result.findings[0].kind == "internal_url"
        assert result.findings[0].action == SecurityAction.BLOCK