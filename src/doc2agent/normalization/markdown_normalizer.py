import re


class MarkdownNormalizer:
    """
    Normalizes Markdown produced by the DOCX converter.

    Important:
    This class fixes structural escaping without blindly removing
    every backslash from the document.
    """

    _heading_pattern = re.compile(
        r"^\*\*(#{1,6}\s+.+?)\*\*\s*$"
    )

    _unordered_list_pattern = re.compile(
        r"^(\s*)\\([-+*])\s+"
    )

    _ordered_list_pattern = re.compile(
        r"^(\s*)(\d+)\\\.\s+"
    )

    def normalize(self, markdown: str) -> str:
        lines = markdown.splitlines()

        normalized_lines = [
            self._normalize_line(line)
            for line in lines
        ]

        result = "\n".join(normalized_lines)

        # Remove excessive blank lines.
        result = re.sub(r"\n{3,}", "\n\n", result)

        return result.strip() + "\n"

    def _normalize_line(self, line: str) -> str:
        line = self._normalize_heading(line)
        line = self._normalize_list_marker(line)
        line = self._normalize_inline_markdown(line)

        return line.rstrip()

    def _normalize_heading(self, line: str) -> str:
        match = self._heading_pattern.match(line)

        if match:
            return match.group(1)

        return line

    def _normalize_list_marker(self, line: str) -> str:
        # \- item -> - item
        line = self._unordered_list_pattern.sub(
            r"\1\2 ",
            line,
        )

        # 1\. item -> 1. item
        line = self._ordered_list_pattern.sub(
            r"\1\2. ",
            line,
        )

        return line

    def _normalize_inline_markdown(self, line: str) -> str:
        """
        Only unescape characters that are meaningful for Markdown.
        """

        replacements = {
            r"\*": "*",
            r"\_": "_",
            r"\(": "(",
            r"\)": ")",
            r"\[": "[",
            r"\]": "]",
            r"\|": "|",
            r"\#": "#",
        }

        for escaped, normal in replacements.items():
            line = line.replace(escaped, normal)

        line = re.sub(
        r"\*{4}(.+?)\*{4}",
        r"**\1**",
        line,
        )

        return line