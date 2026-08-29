# AGENTS.md

## Project Rules

Always work in low-context mode.

- Search before reading and inspect only relevant files or sections.
- Before editing, explain which files will be touched and why.
- Follow the existing architecture and fix the root cause rather than the symptom.
- Do not hardcode values that belong in configuration or existing contracts.
- Do not change unrelated files or expose secrets.
- Preserve Unicode, Hebrew, RTL/BIDI content, stable IDs, source ranges, and existing JSON contracts.
- Keep `Converter` faithful to the source and free of semantic inference.
- Keep `StructuralAnalyzer` deterministic and free of LLM behavior.
- Keep future semantic enrichment isolated in `SemanticAnalyzer` rather than leaking it into conversion or structural analysis.
- Run the narrowest relevant tests and Ruff checks for changed Python behavior.

## Codex Multi-Agent Orchestration

The primary Codex agent is the orchestrator. It owns task understanding, routing, integration, validation, and the final answer.

- Complete small, obvious, local, low-risk work directly when delegation would cost more than it saves.
- Delegate autonomously when a bounded specialist or independent parallel investigation materially improves speed, confidence, or context isolation. The user does not need to request delegation explicitly.
- Use `scout` for narrow read-only repository exploration, execution tracing, dependency discovery, and evidence gathering.
- Use `engineer` for bounded implementation, bug fixes, tests, and focused refactoring. Assign explicit file or module ownership and state that other agents may be working concurrently.
- Use `reviewer` for an independent read-only review when behavior, contracts, several files, security, async state, persistence logic, or regression risk changed. Skip review for trivial edits.
- Use `architect` only for difficult ambiguity, cross-cutting architecture, module boundaries, data contracts, persistence, scalability, or security-sensitive design with long-term consequences.
- Escalate by need: Luna for narrow exploration, Terra for normal engineering and review, Sol for genuinely difficult architecture or reasoning.
- Prefer zero to two subagents. Use three only for genuinely independent workstreams. Do not duplicate investigations or let parallel engineers edit overlapping files.
- Spawn project custom agents without a full-history fork. Give each agent a narrow self-contained task and only the context it needs.
- Ask subagents for concise evidence and results. Wait for required agents, reconcile conflicts, and synthesize one final result.
- Close completed subagent threads when their results are integrated, especially before opening a later specialist at the configured thread limit.
- Subagents must not delegate further. The orchestrator remains accountable for the final implementation and validation.
- Preserve the existing low-context, minimal-change, approval, security, and validation rules when delegating.

