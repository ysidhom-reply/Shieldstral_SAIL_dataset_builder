"""Command-line entry point: `uv run dataset-builder --help`.

Each later step adds one command, following the pattern:
load inputs -> (pilot slice) -> build -> validate Samples -> prepare_out -> write
-> write_counts_report -> check_expected.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from dataset_builder import __version__
from dataset_builder.config import DEFAULT_CONFIG, Config, load_config

app = typer.Typer(help="Build the ipi-shieldstral dataset.", no_args_is_help=True)


@dataclass
class RunOptions:
    cfg: Config
    pilot: bool
    force: bool


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option(help="Path to the YAML config.")] = DEFAULT_CONFIG,
    pilot: Annotated[
        bool, typer.Option("--pilot/--full", help="Pilot on ~pilot_size documents, or full run.")
    ] = True,
    force: Annotated[bool, typer.Option(help="Allow overwriting existing data/out files.")] = False,
    log_level: Annotated[str, typer.Option(help="DEBUG | INFO | WARNING | ERROR")] = "INFO",
) -> None:
    logging.basicConfig(
        level=log_level.upper(), format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    ctx.obj = RunOptions(cfg=load_config(config), pilot=pilot, force=force)


@app.command("check-config")
def check_config(ctx: typer.Context) -> None:
    """Load the config, show the key settings and create missing directories."""
    opts: RunOptions = ctx.obj
    cfg = opts.cfg
    typer.echo(f"seed:       {cfg.seed}")
    typer.echo(f"dataset:    {cfg.dataset.repo_id} (configs: {', '.join(cfg.dataset.configs)})")
    typer.echo(f"run mode:   {'pilot' if opts.pilot else 'full'}  force={opts.force}")
    for name, path in cfg.paths.model_dump().items():
        path.mkdir(parents=True, exist_ok=True)
        typer.echo(f"{name + ':':<11} {path}")
    typer.echo(f"\ninstruct:\n  {cfg.texts.instruct}")
    typer.echo(f"query_template:\n  {cfg.texts.query_template}")


@app.command("p1-bipia-split")
def p1_bipia_split(ctx: typer.Context) -> None:
    """P1: clone BIPIA, deduplicate documents, build the split and the attack table."""
    from dataset_builder.steps import p1_bipia_split as p1

    opts: RunOptions = ctx.obj
    if opts.pilot:
        logging.getLogger(__name__).info("P1 is local and fast (no API): running in full.")
    out = p1.run(opts.cfg, force=opts.force)
    typer.echo(f"BIPIA commit: {out['commit']}")
    typer.echo(f"docs:    {out['docs']}\nattacks: {out['attacks']}\nreport:  {out['report']}")


@app.command("p5-nemotron")
def p5_nemotron(ctx: typer.Context) -> None:
    """P5: Nemotron test set (pairs of original + counterfactual, capped per attacker goal)."""
    from dataset_builder.steps import p5_nemotron as p5

    opts: RunOptions = ctx.obj
    out = p5.run(opts.cfg, force=opts.force, pilot_run=opts.pilot)
    typer.echo(f"Nemotron revision: {out['revision']}")
    typer.echo(f"samples: {out['samples']}")
    typer.echo(f"report:  {out['report']}")
    typer.echo(f"output:  {out['out'] or '(pilot: nothing written to data/out)'}")


@app.command("p2-bipia-positives")
def p2_bipia_positives(
    ctx: typer.Context,
    n_per_doc: Annotated[
        int | None, typer.Option(help="Train/validation samples per document (default: config).")
    ] = None,
) -> None:
    """P2: attacked samples (label 1) for train, validation and test."""
    from dataset_builder.steps import p2_bipia_positives as p2

    opts: RunOptions = ctx.obj
    if opts.pilot:
        logging.getLogger(__name__).info("P2 is local and fast (no API): running in full.")
    out = p2.run(opts.cfg, force=opts.force, n_per_doc=n_per_doc)
    for split, path in out["paths"].items():
        typer.echo(f"{split + ':':<12} {path}")
    typer.echo(f"report:      {out['report']}")


@app.command("p3-benign-pool")
def p3_benign_pool(
    ctx: typer.Context,
    max_rounds: Annotated[
        int | None, typer.Option(help="Regeneration rounds before stopping (default: config).")
    ] = None,
) -> None:
    """P3: benign text pool (Mistral). Pilot by default (20 documents); --full for all."""
    from dataset_builder.steps import p3_benign_pool as p3

    opts: RunOptions = ctx.obj
    out = p3.run(opts.cfg, force=opts.force, is_pilot=opts.pilot, max_rounds=max_rounds)
    typer.echo(f"accepted:  {out['accepted']} texts -> {out['pool']}")
    typer.echo(f"rejected:  {out['rejected']}")
    typer.echo(f"report:    {out['report']}")
    typer.echo(f"cost:      ${out['cost_usd']:.4f}")
    if opts.pilot:
        typer.echo("Pilot done. Read the report and the hand-read file before running --full.")


@app.command("p3-judge-compare")
def p3_judge_compare(
    ctx: typer.Context,
    batch_size: Annotated[int, typer.Option(help="Texts per judge call to test.")] = 20,
) -> None:
    """P3: compare the judge with 1 text per call vs batches, on the pilot's texts."""
    from dataset_builder.steps import p3_judge_compare as cmp

    out = cmp.run(ctx.obj.cfg, batch_size=batch_size)
    single, batch, n = out["attacks"]
    typer.echo(
        f"{'PASS' if out['passed'] else 'FAIL'}: benign agreement {out['agreement']:.1%}, "
        f"attacks flagged {single}/{n} (1 per call) vs {batch}/{n} (batch), "
        f"failed batches {out['failed_batches']}"
    )
    typer.echo(
        f"full-run judge cost estimate: ${out['est'][0]:.2f} (1 per call) vs "
        f"${out['est'][1]:.2f} (batch); this comparison cost ${out['cost_now']:.4f}"
    )
    typer.echo(f"report: {out['report']}")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
