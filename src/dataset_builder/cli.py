"""Command-line entry point: `uv run dataset-builder --help`."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from dataset_builder import __version__
from dataset_builder.config import get_settings
from dataset_builder.pipeline import build as build_dataset

app = typer.Typer(help="Build, validate and export datasets.", no_args_is_help=True)


@app.callback()
def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@app.command()
def build(
    raw_dir: Annotated[Path | None, typer.Option(help="Input folder of *.jsonl files.")] = None,
    out_dir: Annotated[Path | None, typer.Option(help="Output folder.")] = None,
    seed: Annotated[int | None, typer.Option(help="Shuffle seed.")] = None,
) -> None:
    """Run the full pipeline and write train/validation/test splits."""
    s = get_settings()
    build_dataset(
        raw_dir=raw_dir or s.raw_dir,
        out_dir=(out_dir or s.processed_dir) / s.name,
        seed=seed if seed is not None else s.seed,
    )


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
