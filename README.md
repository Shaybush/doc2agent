# Doc2Agent — Python MVP

A ready-to-open VS Code project for the first deterministic Doc2Agent pipeline:

```text
DOC / DOCX / RTF → raw Markdown + media + metadata → StructuralAnalyzer → document.json
```

The current analyzer uses **no LLM**. It preserves Unicode/Hebrew source text and only adds deterministic structure and language/direction metadata.

## 1. Open in VS Code

Unzip the project and open the folder `doc2agent-python` in VS Code.

Recommended extensions:

- Python (Microsoft)
- Ruff

## 2. Create the virtual environment

The easiest way is to run the VS Code task **Terminal → Run Task → Setup project**.

Or from the terminal:

### macOS / Linux

```bash
./scripts/setup.sh
source .venv/bin/activate
```

### Windows PowerShell

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
.venv\Scripts\Activate.ps1
```

Python 3.11+ is required.

## 3. Run tests

```bash
pytest
```

## 4. Test the analyzer immediately

No Word document is required for this test:

```bash
doc2agent analyze examples/sample.md
```

Result:

```text
output/structured/sample.document.json
```

## 5. Run the complete pipeline

Put a `.doc`, `.docx`, or `.rtf` file into `input/`, then run:

```bash
doc2agent pipeline "input/my-document.docx"
```

Output:

```text
output/
├── raw/
│   └── my-document.md
├── media/
│   └── my-document/
├── metadata/
│   └── my-document.json
└── structured/
    └── my-document.document.json
```

## Commands

```bash
doc2agent --help
doc2agent convert input/file.docx
doc2agent analyze output/raw/file.md --metadata output/metadata/file.json
doc2agent pipeline input/file.docx
```

## Current StructuralAnalyzer support

- H1-H6 hierarchy
- deterministic section paths
- paragraphs
- ordered lists
- unordered lists
- fenced code blocks (fence info such as `python`/`ts` is stored separately)
- preamble before first heading
- UTF-8 / Hebrew / mixed-language text
- `ltr` / `rtl` / `mixed` metadata
- deterministic IDs
- source line ranges

## Important separation of responsibilities

### Converter

The converter must not:

- translate text;
- repair or reorder RTL/BIDI text;
- infer business rules;
- rename semantic concepts;
- add AI-generated markup.

It produces faithful raw Markdown, extracted media and minimal metadata. Converter metadata deliberately starts with `language: "und"` and `direction: "auto"`.

### StructuralAnalyzer

The structural analyzer is deterministic and contains no LLM. It converts Markdown structure into canonical `document.json`.

### Later

The next stage will be a `SemanticAnalyzer`, producing richer `knowledge.json`. Only after the document foundation is reliable should RAG/Search and Graph adapters be connected.
