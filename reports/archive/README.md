# Archived reports

Superseded reports, kept for traceability. The current report of each step is in `reports/pN/`;
`reports/working_report.md` summarises the current state.

| File | What it is | Why it is archived |
|---|---|---|
| `p3/p3_pilot_rejections.md`, `.json` | P3 pilot (20 documents) of the original benign pool, prompts p3-v2 and judge-v3 | Superseded by the full run (`p3/p3_rejections.md`) |
| `p3/p3_pilot_handread.md` | Hand-read sample of that pilot | Same |
| `p3/p3_hard_negatives_pilot.md`, `.json` | Pilot (20 documents) of option B, the hard-negative pass, prompts hn-v3 | Superseded by the full pass (`p3/p3_hard_negatives.md`). The prompt history (hn-v1 too few attack words, hn-v2 "in your reply" in tables rejected by the judge) is recorded in `configs/p3_prompts.yaml` |
| `p3/p3_hard_negatives_pilot_handread.md` | Hand-read sample of that pilot | Same |
| `p4/p4_benign_span.md`, `.json` | One-off migration (2026-11-24): the contract added `benign_span`, and it was added to the existing `data/out` files without re-running P4 (every span checked, every other column verified unchanged) | P4 now writes `benign_span` itself and was re-run for option B; the migration command was retired |
| `p5/p5_nemotron_pilot.md`, `.json` | P5 pilot (20 records) | Superseded by the full run (`p5/p5_nemotron.md`) |
| `p10/v0.1/p10_push.md`, `.json`, `p10_dataset_card.md` | Record and dataset card of the v0.1 push to `SAIL-Reply/ipi-twins` (2026-11-23): `bipia` + `nemotron`, 29 columns, original benign pool | `data/out` has changed since (`benign_span`, hard negatives); the next push writes a new `reports/p10/` |
