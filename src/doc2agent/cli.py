from __future__ import annotations

from pathlib import Path

import typer

from doc2agent.analyzer import StructuralAnalyzer
from doc2agent.converter import Converter
from doc2agent.pipeline import Doc2AgentPipeline
from doc2agent.validator import DocumentValidator

app = typer.Typer(
    name="doc2agent",
    help="Convert office documents to Markdown and deterministic document.json.",
    no_args_is_help=True,
)


@app.command()
def convert(
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path("output"), "--output", "-o"),
) -> None:
    result = Converter().convert(source, output)

    typer.echo(f"Markdown: {result.markdown}")
    typer.echo(f"Media:    {result.media_dir}")
    typer.echo(f"Metadata: {result.metadata}")


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


@app.command()
def pipeline(
    source: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
    ),
    output: Path = typer.Option(
        Path("output"),
        "--output",
        "-o",
    ),
) -> None:
    result = Doc2AgentPipeline().run(
        source,
        output,
    )

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
        f"document.json:       {result.document_json}"
    )

    typer.echo(
        f"validation.json:     {result.validation_json}"
    )
