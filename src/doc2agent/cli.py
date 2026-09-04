from __future__ import annotations

from pathlib import Path

import typer
from pydantic import TypeAdapter

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.converter import Converter
from doc2agent.design import (
    DesignAlignmentBuilder,
    DesignBinding,
    FigmaSnapshot,
    FigmaSnapshotStore,
)
from doc2agent.ingest import IngestService
from doc2agent.models.document import AnalyzedDocument
from doc2agent.pipeline import Doc2AgentPipeline
from doc2agent.security import SecurityDecision, SecurityResult
from doc2agent.semantic import (
    ApprovalService,
    CandidateBuilder,
    CandidateReviewer,
    EnrichmentGoldenRunner,
    EnrichmentPatch,
    EnrichmentPatchValidator,
    KnowledgeCandidate,
    OpenAICompatibleEnrichmentProvider,
    SemanticEvaluationRunner,
    TopicRenderer,
    artifact_root_for_document,
    copilot_context_output_path,
    deepseek_settings_from_environment,
    document_artifact_name,
    openai_compatible_settings_from_environment,
)
from doc2agent.support_bundle import SupportBundleExporter
from doc2agent.validator import DocumentValidator, ValidationReport

app = typer.Typer(
    name="doc2agent",
    help="Convert office documents to Markdown and deterministic document.json.",
    no_args_is_help=True,
)


def _copilot_context_output_path(
    approved: KnowledgeCandidate,
    storage_root: Path,
) -> Path:
    manifest = approved.document.source_manifest
    if manifest is None:
        raise ValueError("Approved knowledge is missing its source manifest")
    document_id = IngestService(storage_root).document_id_for_source(manifest.source_id)
    document_key = document_id or manifest.source_id.removeprefix("sha256:")[:12]
    return copilot_context_output_path(approved, storage_root, document_key)


def _storage_root_for_approved_path(approved_path: Path, fallback: Path) -> Path:
    """Infer storage only for snapshots stored under its approved directory."""

    approved_parent = approved_path.parent
    if approved_parent.name == "approved":
        return approved_parent.parent
    if approved_parent.parent.name == "approved":
        return approved_parent.parent.parent
    return fallback


@app.command()
def convert(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path("output"), "--output", "-o"),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    approve_review: bool = typer.Option(False, "--approve-review"),
) -> None:
    ingested = IngestService(storage).ingest(
        source,
        approve_review=approve_review,
    )
    if not ingested.accepted:
        typer.echo(f"Ingest decision: {ingested.decision}")
        for diagnostic in ingested.diagnostics:
            typer.echo(f"{diagnostic.code}: {diagnostic.message}")
        raise typer.Exit(code=1)

    result = Converter().convert(ingested.source_path, output)

    typer.echo(f"Markdown: {result.markdown}")
    typer.echo(f"Media:    {result.media_dir}")
    typer.echo(f"Metadata: {result.metadata}")


@app.command()
def ingest(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    document_id: str | None = typer.Option(None, "--document-id"),
) -> None:
    result = IngestService(storage).ingest(
        source,
        document_id=document_id,
    )

    typer.echo(f"Ingest decision: {result.decision}")
    typer.echo(f"Source ID:       {result.manifest.source_id}")
    for diagnostic in result.diagnostics:
        typer.echo(f"{diagnostic.code}: {diagnostic.message}")

    if not result.accepted:
        raise typer.Exit(code=1)

    typer.echo(f"Document ID:     {result.document_id}")
    typer.echo(f"Stored source:   {result.source_path}")
    typer.echo(f"Source manifest: {result.manifest_path}")
    typer.echo(f"Reused source:   {'yes' if result.duplicate else 'no'}")


@app.command("delete")
def delete_document(
    document_id: str = typer.Option(..., "--document-id"),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    result = IngestService(storage).delete_document(
        document_id,
        confirm=yes,
    )

    typer.echo(f"Document ID: {result.document_id}")
    typer.echo("Affected paths:")
    for path in result.paths:
        typer.echo(f"- {path}")

    if not result.deleted:
        typer.echo("Preview only. Re-run with --yes to move these files to trash.")
        return

    typer.echo(f"Moved to trash: {result.trash_path}")


@app.command("export-support")
def export_support(
    document_id: str = typer.Option(..., "--document-id"),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Create a portable diagnostic ZIP without document content or secrets."""

    try:
        bundle = SupportBundleExporter(storage).export(
            document_id,
            output=output,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Support bundle: {bundle}")


@app.command()
def analyze(
    markdown: Path = typer.Argument(..., exists=True, dir_okay=False),
    metadata: Path | None = typer.Option(None, "--metadata", "-m"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    analyzer = StructuralAnalyzer()

    document = analyzer.analyze(
        markdown,
        metadata_path=metadata,
    )

    output_path = (
        output
        or Path("output/structured")
        / f"{markdown.stem}.document.json"
    )

    analyzer.write_json(
        document,
        output_path,
    )

    typer.echo(
        f"document.json: {output_path}"
    )


@app.command()
def validate(
    document_json: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
    ),
) -> None:
    validator = DocumentValidator()

    report = validator.validate_json(
        document_json
    )

    output_path = (
        output
        or Path("output/validation")
        / document_json.name.replace(
            ".document.json",
            ".validation.json",
        )
    )

    validator.write_json(
        report,
        output_path,
    )

    typer.echo(
        f"validation.json: {output_path}"
    )

    if report.valid:
        typer.echo("Validation: OK")
    else:
        typer.echo("Validation: FAILED")

        for issue in report.errors:
            typer.echo(
                f"ERROR {issue.code}: {issue.message}"
            )

        raise typer.Exit(code=1)

    for issue in report.warnings:
        typer.echo(
            f"WARNING {issue.code}: {issue.message}"
            )


@app.command("validate-patch")
def validate_patch(
    patch: Path = typer.Argument(..., exists=True, dir_okay=False),
    document: Path = typer.Option(..., "--document", exists=True, dir_okay=False),
) -> None:
    report = EnrichmentPatchValidator().validate_json(patch, document)
    typer.echo(f"Patch validation: {'OK' if report.valid else 'FAILED'}")
    for issue in report.errors:
        typer.echo(f"ERROR {issue.code}: {issue.message}")

    if not report.valid:
        raise typer.Exit(code=1)


@app.command("create-candidate")
def create_candidate(
    patch: Path = typer.Argument(..., exists=True, dir_okay=False),
    document: Path = typer.Option(..., "--document", exists=True, dir_okay=False),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    patch_model = EnrichmentPatch.model_validate_json(
        patch.read_text(encoding="utf-8")
    )
    document_model = AnalyzedDocument.model_validate_json(document.read_text(encoding="utf-8"))
    candidate = CandidateBuilder().build(
        patch_model,
        document_model,
    )
    output_path = output or (
        artifact_root_for_document(document, candidate.source_id)
        / "candidate"
        / f"{candidate.candidate_sha256}.knowledge.json"
    )
    CandidateBuilder.write_json(candidate, output_path)
    typer.echo(f"candidate knowledge.json: {output_path}")
    typer.echo(f"Candidate hash: {candidate.candidate_sha256}")


@app.command("review-candidate")
def review_candidate(
    candidate: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    candidate_model = KnowledgeCandidate.model_validate_json(
        candidate.read_text(encoding="utf-8")
    )
    review = CandidateReviewer().review(candidate_model)
    typer.echo(CandidateReviewer.render(review))


@app.command("import-figma-snapshot")
def import_figma_snapshot(
    snapshot: Path = typer.Argument(..., exists=True, dir_okay=False),
    source_id: str = typer.Option(..., "--source-id"),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
) -> None:
    """Validate and store one read-only Figma snapshot for a source revision."""

    if IngestService(storage).document_id_for_source(source_id) is None:
        raise typer.BadParameter("Figma snapshot source is not registered")
    try:
        snapshot_model = FigmaSnapshot.model_validate_json(snapshot.read_text(encoding="utf-8"))
        output_path = FigmaSnapshotStore(storage).write(source_id, snapshot_model)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Figma snapshot: {output_path}")
    typer.echo(f"Snapshot hash: {snapshot_model.snapshot_sha256}")


@app.command("create-design-alignment")
def create_design_alignment(
    bindings: Path = typer.Argument(..., exists=True, dir_okay=False),
    candidate: Path = typer.Option(..., "--candidate", exists=True, dir_okay=False),
    snapshot_sha256: str = typer.Option(..., "--snapshot-sha256"),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
) -> None:
    """Create a reviewable requirement-to-Figma mapping from explicit decisions."""

    try:
        candidate_model = KnowledgeCandidate.model_validate_json(
            candidate.read_text(encoding="utf-8")
        )
        if IngestService(storage).document_id_for_source(candidate_model.source_id) is None:
            raise ValueError("Design alignment source is not registered")
        snapshot = FigmaSnapshotStore(storage).load(
            candidate_model.source_id,
            snapshot_sha256,
        )
        bindings_model = TypeAdapter(list[DesignBinding]).validate_json(
            bindings.read_text(encoding="utf-8")
        )
        alignment = DesignAlignmentBuilder().build(
            candidate_model,
            snapshot,
            bindings_model,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    output_path = (
        storage
        / "artifacts"
        / candidate_model.source_id.replace(":", "-")
        / "design-alignment"
        / f"{alignment.alignment_sha256}.design-alignment.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        alignment.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    typer.echo(f"Design alignment: {output_path}")
    typer.echo(f"Alignment hash: {alignment.alignment_sha256}")


@app.command("render-copilot")
def render_copilot(
    approved: Path | None = typer.Option(
        None,
        "--approved",
        dir_okay=False,
    ),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    document_id: str | None = typer.Option(None, "--document-id"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    snapshot_storage = (
        _storage_root_for_approved_path(approved, storage)
        if approved is not None
        else storage
    )
    approval_service = ApprovalService(snapshot_storage)
    approved = approved or approval_service.resolve_current_path(document_id)
    if not approved.is_file():
        raise typer.BadParameter(f"Approved knowledge was not found: {approved}")
    approved_model = KnowledgeCandidate.model_validate_json(
        approved.read_text(encoding="utf-8")
    )
    catalog_document_id = IngestService(snapshot_storage).document_id_for_source(
        approved_model.source_id
    )
    if catalog_document_id is None:
        raise typer.BadParameter("Approved knowledge source is not registered")
    if document_id is not None and document_id != catalog_document_id:
        raise typer.BadParameter("--document-id does not match the approved snapshot")
    try:
        approval_service.require_approved_snapshot(
            approved,
            approved_model,
            document_id=catalog_document_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    output = output or _copilot_context_output_path(
        approved_model,
        snapshot_storage,
    )
    result = TopicRenderer().render(approved_model, output)
    typer.echo(f"Copilot context: {result.output_root}")
    typer.echo(f"Knowledge Markdown: {result.context_path}")
    typer.echo(f"Manifest: {result.manifest_path}")


@app.command("enrich")
def enrich(
    document: Path = typer.Argument(..., exists=True, dir_okay=False),
    validation: Path = typer.Option(..., "--validation", exists=True, dir_okay=False),
    security: Path = typer.Option(..., "--security", exists=True, dir_okay=False),
    provider: str = typer.Option("deepseek", "--provider"),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    api_key_environment: str = typer.Option("DEEPSEEK_API_KEY", "--api-key-env"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    original_document = AnalyzedDocument.model_validate_json(
        document.read_text(encoding="utf-8")
    )
    validation_report = ValidationReport.model_validate_json(
        validation.read_text(encoding="utf-8")
    )
    security_result = SecurityResult.model_validate_json(
        security.read_text(encoding="utf-8")
    )
    if not validation_report.valid:
        raise typer.BadParameter("Document validation must pass before enrichment")
    if security_result.decision == SecurityDecision.BLOCK:
        raise typer.BadParameter("Security review blocked this document from model use")

    sanitized_document = AnalyzedDocument.model_validate(security_result.sanitized_document)
    if (
        original_document.source_manifest != sanitized_document.source_manifest
        or original_document.structural_sha256 != sanitized_document.structural_sha256
    ):
        raise typer.BadParameter("Security artifact belongs to a different document revision")

    try:
        if provider == "deepseek":
            settings = deepseek_settings_from_environment(
                model=model,
                base_url=base_url,
            )
        else:
            if model is None or base_url is None:
                raise typer.BadParameter(
                    "A non-DeepSeek provider requires --model and --base-url"
                )
            settings = openai_compatible_settings_from_environment(
                name=provider,
                api_key_environment=api_key_environment,
                base_url=base_url,
                model=model,
            )
        patch = OpenAICompatibleEnrichmentProvider(settings).create_patch(
            sanitized_document
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"Enrichment failed: {exc}")
        raise typer.Exit(code=1) from exc

    output_path = output or (
        artifact_root_for_document(document, patch.source_id)
        / "enrichment"
        / f"{document_artifact_name(document)}.enrichment-patch.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(patch.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"enrichment-patch.json: {output_path}")
    typer.echo(f"Provider: {settings.name}")
    typer.echo(f"Model: {settings.model}")


@app.command("evaluate-enrichment")
def evaluate_enrichment(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    golden: Path = typer.Option(..., "--golden", exists=True, dir_okay=False),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    approve_review: bool = typer.Option(False, "--approve-review"),
    provider: str = typer.Option("deepseek", "--provider"),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    api_key_environment: str = typer.Option("DEEPSEEK_API_KEY", "--api-key-env"),
) -> None:
    """Run one provider patch against a v2 golden fixture."""

    ingest_service = IngestService(storage)
    ingested = ingest_service.ingest(source, approve_review=approve_review)
    if not ingested.accepted:
        raise typer.BadParameter(
            "DOCX intake must pass before enrichment evaluation"
        )

    artifact_root = storage / "artifacts" / ingested.manifest.source_id.replace(":", "-")
    pipeline_result = Doc2AgentPipeline().run(ingested, artifact_root)
    ingest_service.record_artifact_root(ingested.manifest.source_id, artifact_root)
    if not pipeline_result.can_send_to_llm:
        raise typer.BadParameter(
            f"Pipeline status {pipeline_result.status} cannot be sent to a model"
        )

    sanitized_document = AnalyzedDocument.model_validate(
        pipeline_result.security.sanitized_document
    )
    try:
        if provider == "deepseek":
            settings = deepseek_settings_from_environment(
                model=model,
                base_url=base_url,
            )
        else:
            if model is None or base_url is None:
                raise typer.BadParameter(
                    "A non-DeepSeek provider requires --model and --base-url"
                )
            settings = openai_compatible_settings_from_environment(
                name=provider,
                api_key_environment=api_key_environment,
                base_url=base_url,
                model=model,
            )
        patch = OpenAICompatibleEnrichmentProvider(settings).create_patch(
            sanitized_document
        )
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(f"Enrichment failed: {exc}") from exc

    report = EnrichmentGoldenRunner().evaluate_patch(
        patch,
        sanitized_document,
        golden,
    )
    typer.echo(report.model_dump_json())
    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def approve(
    candidate: Path = typer.Argument(..., exists=True, dir_okay=False),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    document_id: str | None = typer.Option(None, "--document-id"),
    approved_by: str = typer.Option(..., "--by"),
) -> None:
    candidate_model = KnowledgeCandidate.model_validate_json(
        candidate.read_text(encoding="utf-8")
    )
    catalog_document_id = IngestService(storage).document_id_for_source(
        candidate_model.source_id
    )
    if document_id is not None and catalog_document_id not in (None, document_id):
        raise typer.BadParameter("--document-id does not match the candidate source")
    resolved_document_id = catalog_document_id or document_id
    if resolved_document_id is None:
        raise typer.BadParameter(
            "Candidate source is not registered; pass --document-id"
        )
    current_path = ApprovalService(storage).approve(
        candidate_model,
        document_id=resolved_document_id,
        approved_by=approved_by,
    )
    typer.echo(f"Approved knowledge.json: {current_path}")
    typer.echo(f"Candidate hash: {candidate_model.candidate_sha256}")


@app.command()
def rollback(
    candidate_hash: str = typer.Option(..., "--candidate-hash"),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    document_id: str = typer.Option(..., "--document-id"),
    approved_by: str = typer.Option(..., "--by"),
) -> None:
    current_path = ApprovalService(storage).rollback(
        candidate_hash,
        document_id=document_id,
        approved_by=approved_by,
    )
    typer.echo(f"Rolled back approved knowledge.json: {current_path}")
    typer.echo(f"Candidate hash: {candidate_hash}")


@app.command("evaluate-semantic")
def evaluate_semantic(
    markdown: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
    ),
    cases: Path = typer.Option(
        ...,
        "--cases",
        exists=True,
        dir_okay=False,
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
    ),
) -> None:
    runner = SemanticEvaluationRunner()
    report = runner.run(markdown, cases)
    output_path = (
        output
        or Path("output/evaluation")
        / f"{markdown.stem}.semantic-evaluation.json"
    )
    runner.write_json(report, output_path)

    typer.echo(f"semantic evaluation: {output_path}")
    typer.echo(f"Provider: {report.provider}")
    typer.echo(
        f"Matched: {len(report.matched_case_ids)}/{report.expected_count}"
    )
    typer.echo(f"Evaluation: {'OK' if report.passed else 'FAILED'}")

    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def pipeline(
    source: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
    ),
    storage: Path = typer.Option(Path("storage"), "--storage", "-s"),
    approve_review: bool = typer.Option(False, "--approve-review"),
) -> None:
    ingest_service = IngestService(storage)
    ingested = ingest_service.ingest(
        source,
        approve_review=approve_review,
    )

    typer.echo(f"Ingest decision: {ingested.decision}")
    for diagnostic in ingested.diagnostics:
        typer.echo(f"{diagnostic.code}: {diagnostic.message}")
    if not ingested.accepted:
        if ingested.decision.value == "requires_review":
            typer.echo("Re-run with --approve-review to process without opening links.")
        raise typer.Exit(code=1)

    if ingested.review_approved:
        typer.echo("Review approval: external links will not be opened.")

    source_key = ingested.manifest.source_id.replace(":", "-")
    artifact_root = storage / "artifacts" / source_key
    result = Doc2AgentPipeline().run(
        ingested,
        artifact_root,
    )
    ingest_service.record_artifact_root(
        ingested.manifest.source_id,
        artifact_root,
    )

    typer.echo(f"Document ID:         {ingested.document_id}")
    typer.echo(f"Artifact root:       {artifact_root}")

    typer.echo(
        f"Raw Markdown:        {result.converted.markdown}"
    )

    typer.echo(
        f"Normalized Markdown: {result.normalized_markdown}"
    )

    typer.echo(
        f"Metadata:            {result.converted.metadata}"
    )

    typer.echo(
        f"Source manifest:     {result.source_manifest_json}"
    )

    typer.echo(
        f"document.json:       {result.document_json}"
    )

    typer.echo(
        f"validation.json:     {result.validation_json}"
    )

    validation_status = "OK" if result.validation.valid else "FAILED"

    typer.echo(
        f"Validation:          {validation_status}"
    )

    typer.echo(
        f"security.json:       {result.security_json}"
    )

    typer.echo(
        f"Pipeline status:      {result.status}"
    )
