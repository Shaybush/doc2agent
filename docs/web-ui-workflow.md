# Web UI workflow

The Web UI is a local operator surface for the existing Doc2Agent domain
workflow, not a second implementation.

## Start

```bash
uv run uvicorn doc2agent.web.app:app --reload
```

Open <http://127.0.0.1:8000>.

## Lifecycle

```text
Upload → deterministic pipeline → Generate candidate → proof review
→ Approve and publish → final Copilot Markdown
```

- Upload runs `IngestService` and `Doc2AgentPipeline`; it displays collapsed
  normalized Markdown and sanitized structured JSON.
- Generate candidate uses server-side provider configuration. DeepSeek reads
  `DEEPSEEK_API_KEY` from the process environment; the browser never sends a
  key.
- Proof review shows every fact with exact evidence text, section path, and
  source-line range.
- Approval requires a named reviewer, invokes `ApprovalService`, updates the
  approved snapshot atomically, and renders final Copilot Markdown.

Long actions show HTMX loaders. Code/JSON previews are collapsed by default.
Final Copilot Markdown appears only after the current UI run approves and
publishes it; an older published artifact is not auto-displayed at upload.

## Next quality gate

The deterministic pipeline works without a model. Candidate generation is an
explicit networked action. Before treating a provider as trusted by default,
run it on the semantic evaluation fixture and a representative 5–10 DOCX corpus
with manually checked evidence. Only then consider cache, resume, background
jobs, or durable task state.
