"""Command-line entry point: `uv run dataset-builder --help`.

Each later step adds one command, following the pattern:
load inputs -> (pilot slice) -> build -> validate Samples -> prepare_out -> write
-> write_counts_report -> check_expected.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from dataset_builder import __version__
from dataset_builder.config import DEFAULT_CONFIG, Config, load_config

app = typer.Typer(help="Build the ipi-twins dataset.", no_args_is_help=True)


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
    typer.echo(f"\ninstruct (prompt text, not a column):\n  {cfg.texts.instruct}")
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
    hard_negatives: Annotated[
        bool,
        typer.Option(
            "--hard-negatives",
            help="Option B: replace part of every pool with harmless instruction-like texts.",
        ),
    ] = False,
) -> None:
    """P3: benign text pool (Mistral). Pilot by default (20 documents); --full for all."""
    from dataset_builder.steps import p3_benign_pool as p3

    opts: RunOptions = ctx.obj
    if hard_negatives:
        from dataset_builder.steps import p3_hard_negatives as hn

        out = hn.run(opts.cfg, is_pilot=opts.pilot)
        c = out["counts"]
        typer.echo(
            f"hard negatives: {c.get('new', 0)} new, {c.get('replaced', 0)} replaced, "
            f"{c.get('restored', 0)} put back -> {out['pool']}"
        )
        typer.echo(f"judge-v4 control: {out['control']['flagged']}/{out['control']['n']}")
        typer.echo(f"report:    {out['report']}")
        typer.echo(f"cost:      ${out['cost_usd']:.4f}")
        if opts.pilot:
            typer.echo("Pilot done. Read the report and the hand-read file before running --full.")
        return
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


@app.command("p4-bipia-assemble")
def p4_bipia_assemble(ctx: typer.Context) -> None:
    """P4: negatives (label 0) for every P2 positive; writes the bipia config to data/out."""
    from dataset_builder.steps import p4_bipia_assemble as p4

    opts: RunOptions = ctx.obj
    if opts.pilot:
        logging.getLogger(__name__).info("P4 is local and fast (no API): running in full.")
    out = p4.run(opts.cfg, force=opts.force)
    for split, path in out["paths"].items():
        typer.echo(f"{split + ':':<12} {path}")
    typer.echo(f"pairs:       {out['pairs']} ({out['fallback']} closest-length fallbacks)")
    typer.echo(f"report:      {out['report']}")


@app.command("p6-textfooler")
def p6_textfooler(ctx: typer.Context) -> None:
    """P6: TextFooler test variants against base Shieldstral (needs a GPU). Pilot by default."""
    from dataset_builder.steps import p6_textfooler as p6

    opts: RunOptions = ctx.obj
    out = p6.run(opts.cfg, force=opts.force, is_pilot=opts.pilot)
    typer.echo(f"results: {out['results']}")
    typer.echo(f"report:  {out['report']}")
    if opts.pilot:
        pj = out["projection"]
        typer.echo(
            "projected full run: "
            + (f"{pj['total_hours']:.1f} h" if pj["total_hours"] is not None else "unknown")
            + (" - UNREASONABLE, do not run --full" if pj["unreasonable"] else "")
        )
        typer.echo("Pilot done. Read the report, set attempt_cap, then run --full.")
    else:
        typer.echo(f"output:  {out['out']}")
        typer.echo(f"human check: {out['human_check']}")


@app.command("p6-calibrate-judge")
def p6_calibrate_judge(
    set_b: Annotated[
        Path | None, typer.Option("--set-b", help="Hand-labelled CSV for set B (see the report).")
    ] = None,
) -> None:
    """P6: calibrate the local judge on set A (labels of originals) and set B (hand-labelled)."""
    from dataset_builder.steps import p6_calibrate

    out = p6_calibrate.run(set_b=set_b)
    typer.echo(f"report: {out['report']}")


@app.command("p6-agreement")
def p6_agreement(
    path: Annotated[
        Path | None, typer.Argument(help="human_check.csv (default: reports/p6)")
    ] = None,
) -> None:
    """P6: agreement between the person's verdicts and the judge's in human_check.csv."""
    from dataset_builder.config import load_config
    from dataset_builder.steps.p6_report import human_agreement

    path = path or load_config().paths.reports / "p6" / "human_check.csv"
    res = human_agreement(path)
    typer.echo(json.dumps(res, indent=2))


@app.command("p7-checks")
def p7_checks(
    ctx: typer.Context,
    version: Annotated[str, typer.Option(help="v0, v1, v2 or v3 (v0 to v2 run now).")] = "v1",
) -> None:
    """P7: data checks, shortcut baselines and diagnostics for one version (reads data/out only).
    Exit 0 = PUSH ALLOWED, 1 = a data check failed, 2 = only a baseline limit is exceeded."""
    from dataset_builder.steps import p7_checks as p7

    out = p7.run(ctx.obj.cfg, version=version)
    for c in out["checks"]:
        typer.echo(f"{c['status']:<5} {c['config']:<16} {c['check']}")
    b = out["baselines"]
    for name, by_eval in b.items():
        for ev, groups in by_eval.items():
            typer.echo(f"{'':<5} baseline {name:<6} on {ev:<14} AUC {groups['all']['auc']:.3f}")
    for s in out["stop"]:
        typer.echo(f"LIMIT {s}")
    typer.echo(f"report: {out['report']}")
    typer.echo(out["status"])
    raise typer.Exit(out["exit_code"])


@app.command("p10-push")
def p10_push(
    ctx: typer.Context,
    push: Annotated[
        bool, typer.Option("--push", help="Upload to Hugging Face. Without it: dry-run only.")
    ] = False,
    version: Annotated[
        str,
        typer.Option(
            "--version", help="v0, v1 (bipia + nemotron, lean columns) or v2 (+ textfooler)."
        ),
    ] = "v1",
    card_only: Annotated[
        bool,
        typer.Option(
            "--card-only",
            help="With --push: upload only README.md as a new commit on main (no data, no tag).",
        ),
    ] = False,
) -> None:
    """P10: preflight, dataset card, staging, secret scan and the P7 gate for one version.
    Uploads only with --push, and only if P7 says PUSH ALLOWED for that version."""
    from dataset_builder.steps import p10_push as p10

    opts: RunOptions = ctx.obj
    out = p10.run(opts.cfg, version=version, do_push=push, card_only=card_only)
    for name, st in out["files"].items():
        typer.echo(f"{name + ':':<26} {st['rows']:>7,} rows")
    typer.echo(
        "uploads:  "
        + (", ".join(out["uploads"]) + " + the card" if out["uploads"] else "the card only")
        + " (other configs untouched)"
    )
    typer.echo(f"card:     {out['card']}")
    typer.echo(f"staged:   {out['stage_dir']}")
    typer.echo("secrets:  none found in the staged files")
    typer.echo(
        f"P7 gate:  {out['gate']['status']}"
        + (f" ({out['gate']['reason']})" if out["gate"]["reason"] else "")
    )
    typer.echo(f"report:   {out['report']}")
    if push and card_only:
        typer.echo(
            f"card updated: {opts.cfg.dataset.repo_id} @ {out['card_push']['commit']} "
            f"(README.md only, tag {version} not moved)"
        )
    elif push:
        typer.echo(
            f"pushed:   {opts.cfg.dataset.repo_id} @ {out['push']['commit']} (tag {version})"
        )
        typer.echo(f"Next: uv run dataset-builder p10-verify --version {version}")
    else:
        typer.echo(
            "Dry-run: nothing was uploaded. Read the card, confirm the data is final, then run "
            f"with --push --version {version} to upload to {opts.cfg.dataset.repo_id}."
        )


@app.command("p10-verify")
def p10_verify(
    ctx: typer.Context,
    version: Annotated[str, typer.Option("--version", help="Tag to load: v0, v1 or v2.")] = "v1",
) -> None:
    """P10: load every config and split at the version's tag in a clean environment and check
    the counts against the reports (from v1: the previous tag still loads, and the files not
    re-uploaded are unchanged)."""
    from dataset_builder.steps import p10_push as p10

    out = p10.run_verify(ctx.obj.cfg, version=version)
    for name, st in out["loaded"].items():
        typer.echo(f"{name + ':':<26} {st['rows']:>7,} rows loaded")
    prev = out["previous_unchanged"]
    if prev:
        typer.echo(f"{prev['tag']} still loads; {len(prev['files_compared'])} files identical")
    typer.echo(f"private: {out['private']}  |  secrets in remote files: none")
    typer.echo(f"PASSED. report: {out['report']}")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
