from __future__ import annotations

from doc2agent.models.document import Section


def build_section_tree(flat_sections: list[Section]) -> list[Section]:
    """Turn document-order sections into an H1-H6 tree."""

    roots: list[Section] = []
    stack: list[Section] = []

    for section in flat_sections:
        while stack and stack[-1].level >= section.level:
            stack.pop()

        if stack:
            stack[-1].children.append(section)
        else:
            roots.append(section)

        stack.append(section)

    return roots
