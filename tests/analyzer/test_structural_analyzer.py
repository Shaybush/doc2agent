from doc2agent.analyzer import StructuralAnalyzer


def test_builds_h1_h6_tree_and_paths():
    markdown = """# Root
root text
## Child
child text
### Grandchild
value
## Sibling
end
"""
    document = StructuralAnalyzer().analyze_text(markdown)

    root = document.sections[0]
    assert root.level == 1
    assert root.path == ["Root"]
    assert [child.title for child in root.children] == ["Child", "Sibling"]
    assert root.children[0].children[0].path == ["Root", "Child", "Grandchild"]
    assert root.end_line == 8
    assert root.children[0].end_line == 6


def test_preserves_hebrew_and_marks_rtl():
    markdown = """# מסמך
## אימות משתמש
יש לבדוק שהמשתמש מחובר לפני ביצוע הפעולה.
"""
    document = StructuralAnalyzer().analyze_text(markdown)

    child = document.sections[0].children[0]
    assert child.title == "אימות משתמש"
    assert child.direction == "rtl"
    assert child.blocks[0].text == "יש לבדוק שהמשתמש מחובר לפני ביצוע הפעולה."
    assert child.blocks[0].direction == "rtl"


def test_parses_ordered_unordered_lists_and_code_fence():
    markdown = """# Test
- one
- two

1. first
2. second

```python
# not a heading
print('ok')
```
"""
    document = StructuralAnalyzer().analyze_text(markdown)
    blocks = document.sections[0].blocks

    assert [block.type for block in blocks] == ["unordered_list", "ordered_list", "code"]
    assert blocks[0].items == ["one", "two"]
    assert blocks[1].items == ["first", "second"]
    assert "# not a heading" in blocks[2].text
    assert blocks[2].info == "python"
    assert len(document.sections) == 1


def test_preamble_is_kept():
    markdown = """Before heading.

# Title
After.
"""
    document = StructuralAnalyzer().analyze_text(markdown)
    assert document.preamble[0].text == "Before heading."
    assert document.sections[0].blocks[0].text == "After."


def test_ids_are_deterministic():
    analyzer = StructuralAnalyzer()
    first = analyzer.analyze_text("# A\nText", markdown_path="a.md")
    second = analyzer.analyze_text("# A\nText", markdown_path="a.md")
    assert first.document.id == second.document.id
    assert first.sections[0].id == second.sections[0].id
    assert first.sections[0].blocks[0].id == second.sections[0].blocks[0].id
