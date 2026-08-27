from doc2agent.normalization.markdown_normalizer import MarkdownNormalizer


def test_normalizes_heading():
    normalizer = MarkdownNormalizer()

    result = normalizer.normalize(
        "**# 1. מבוא**"
    )

    assert result == "# 1. מבוא\n"


def test_normalizes_unordered_list():
    normalizer = MarkdownNormalizer()

    result = normalizer.normalize(
        r"\- שמירה על היררכיית כותרות."
    )

    assert result == "- שמירה על היררכיית כותרות.\n"


def test_normalizes_ordered_list():
    normalizer = MarkdownNormalizer()

    result = normalizer.normalize(
        r"1\. Converter creates Markdown."
    )

    assert result == "1. Converter creates Markdown.\n"


def test_normalizes_bold():
    normalizer = MarkdownNormalizer()

    result = normalizer.normalize(
        r"**\*\*טקסט מודגש\*\***"
    )

    assert result == "**טקסט מודגש**\n"