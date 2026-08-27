# document.json schema (v1.0)

`document.json` is the deterministic internal representation produced from raw Markdown.

Top level:

- `schema_version`
- `document`
- `preamble`
- `sections`

Each section contains:

- stable `id`
- Markdown heading `level` (1..6)
- original `title`
- hierarchical `path`
- `language` and `direction` metadata
- `start_line` / `end_line`
- direct content `blocks`
- nested `children`

Supported block types in the first version:

- `paragraph`
- `unordered_list`
- `ordered_list`
- `code`

The analyzer never translates or rewrites source text.
