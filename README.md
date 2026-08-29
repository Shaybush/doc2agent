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

## 5. Accept a DOCX safely

Before processing a DOCX, save an immutable application-owned copy:

```bash
doc2agent ingest "input/my-document.docx"
```

The command checks the DOCX package without extracting it. A document with
macros, a corrupt ZIP package, or unsafe archive paths is blocked. External
relationships require review and are never fetched. Accepted files are stored
by content hash in `storage/sources/`, and `storage/source-catalog.json` maps
their versions to a logical document ID.

When an edited DOCX is accepted again from the same path, it automatically
becomes the next version of that logical document. `--document-id` is only an
advanced override for a renamed or moved file that must be connected to an
existing document history.

## 6. Run the complete pipeline

The safe complete pipeline currently accepts **DOCX**. `Converter` also supports
DOC and RTF when used directly, but they do not have the DOCX preflight safety
guarantee yet. Put a `.docx` file into `input/`, then run:

```bash
doc2agent pipeline "input/my-document.docx"
```

Output:

```text
storage/artifacts/sha256-.../
├── raw/
│   └── my-document.md
├── media/
│   └── my-document/
├── metadata/
│   └── my-document.json
├── normalized/
│   └── my-document.md
├── structured/
│   └── my-document.document.json
├── validation/
│   └── my-document.validation.json
└── security/
    └── my-document.security.json
```

## 7. Use the Web UI

```bash
uv run uvicorn doc2agent.web.app:app --reload
```

Open <http://127.0.0.1:8000>. The UI follows the existing workflow:

```text
Upload DOCX → deterministic artifacts → Generate candidate → proof review
→ Approve and publish → final Copilot Markdown
```

Candidate generation reads the existing server-side `DEEPSEEK_API_KEY`; the
browser never provides a key. The final Markdown appears only after the current
UI run explicitly approves and publishes its candidate. See
[Web UI workflow](docs/web-ui-workflow.md).

## Commands

```bash
doc2agent --help
doc2agent convert input/file.docx
doc2agent analyze output/raw/file.md --metadata output/metadata/file.json
doc2agent ingest input/file.docx
doc2agent pipeline input/file.docx
doc2agent delete --document-id document:... --yes
doc2agent export-support --document-id document:...
doc2agent evaluate-semantic tests/fixtures/semantic-evaluation.md --cases tests/fixtures/semantic-evaluation-cases.json
```

## Offline diagnostics

On an isolated computer, export a diagnostic ZIP after an error. It contains
application/runtime details, hashes, validation and security summaries, but does
not contain the source DOCX, Markdown, `document.json`, approved knowledge,
`sanitized_document` payload, or raw error logs.

```bash
doc2agent export-support --document-id "document:..."
```

The command prints the ZIP path under `support-bundles/`. Review the ZIP
according to your transfer policy before moving it to another system. If a raw
traceback is also needed, review and transfer it separately under that policy;
it is deliberately not added to the bundle. If reproducing a problem requires
document content, create a separate approved anonymized fixture rather than
adding the original DOCX to the bundle.

If `pipeline` reports `requires_review` because of an external relationship,
inspect the warning and explicitly continue without opening the link:

```bash
doc2agent pipeline input/file.docx --approve-review
```

Validate a future provider/LLM proposal without applying it:

```bash
doc2agent validate-patch patch.json --document structured.document.json
doc2agent create-candidate patch.json --document structured.document.json
doc2agent review-candidate candidate.knowledge.json
doc2agent approve candidate.knowledge.json --by "george"
doc2agent render-copilot
```

When more than one document has approved knowledge, select the one to render:

```bash
doc2agent render-copilot --document-id "document:..."
```

`pipeline`, `enrich`, and `create-candidate` keep generated artifacts inside
`storage/artifacts/<source-version>/`. The user-facing published result is
`storage/copilot-context/<source-name>--<document-id>/<source-name>.md`; do not
manually edit it. A new version of the same logical document replaces only its
own generated context directory, while documents with the same filename remain
separate.

Generate a proposed patch only from a validated, security-approved document:

```bash
doc2agent enrich structured.document.json \
  --validation validation.json \
  --security security.json
```

DeepSeek is the default profile. Set `DEEPSEEK_API_KEY` outside the repository.
For any other OpenAI-compatible API, supply a provider name, model, base URL,
and the name of its environment variable (never the key itself):

```bash
doc2agent enrich structured.document.json \
  --validation validation.json \
  --security security.json \
  --provider local \
  --model my-model \
  --base-url http://localhost:8000/v1 \
  --api-key-env LOCAL_MODEL_API_KEY
```

## Documentation

- [Architecture](docs/architecture.md)
- [Pipeline and artifact contract](docs/pipeline-and-artifacts.md)
- [Canonical `document.json` contract](docs/document-json.md)
- [Optional `knowledge.json` contract](docs/knowledge-json.md)
- [`EnrichmentPatch` contract](docs/enrichment-patch.md)
- [Web UI workflow](docs/web-ui-workflow.md)
- [Implementation roadmap](docs/implementation-roadmap-ru.md)

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

The deterministic pipeline makes no LLM calls. Semantic enrichment is a
separate explicit action: a provider may propose an evidence-linked candidate,
but only human approval can publish Copilot Markdown. The built-in DeepSeek
adapter is not an automatic pipeline stage and needs a quality gate before it
becomes a trusted production default.
