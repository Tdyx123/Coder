# Evidence index

These files are curated from immutable local runs. `full-report.json.xz` is a lossless
compressed copy of each original report. The SHA-256 values below are hashes of the
tracked files, not of their decompressed payloads.

| Directory / file | Meaning | SHA-256 |
|---|---|---|
| `full-baseline/full-report.json.xz` | 120-run pre-optimization full baseline at `e82e20f5` | `0bd5b97ab4956593a21837444c4054f03c86e3faa51d58bbf9fdcdc849858980` |
| `full-baseline/report.json` | Readable baseline evidence; corrected check is false and original true is retained | `3b9d76c440c077543345b668575e88a67de636e06ef3ae53fd36d77913b1f270` |
| `full-baseline/gate-recheck.json` | Authoritative failed baseline gate | `06e3808df03dcfef5ccff48a2303b3b039895203a98ed47315795829831e0610` |
| `full-baseline/batch2-comparison.json` | Batch 2 comparison diagnostic | `b2b3067d1b83864592a58b8aa8e960511f7c10f22b38a0222f097623f9083dee` |
| `event-smoke-21019fc0/full-report.json.xz` | Initial 24-run event smoke before budget-regression repair | `42126afd65584d01b4a91ade15313755671bcc7d7721f593f9195a7c039f02cc` |
| `event-smoke-21019fc0/report.json` | Readable initial smoke evidence | `d138d79304ad9860f54a01677af6bb28da372d4341e17a715a6cb7b6fa7e6d09` |
| `event-smoke-b3aa308f/full-report.json.xz` | Corrected 24-run full/event smoke | `0c4a8af1e8ab1c886d1bc0e0e26a4de346c93e84c8978a8d73540873470bc129` |
| `event-smoke-b3aa308f/report.json` | Readable corrected smoke; correctness matched, performance gate failed | `eeec70a9fc93379a92b825e042137770eb66dbef63ed5fd6acc26587f9be86bf` |
| `old-navigation-replay/full-report.json.xz` | Five-run old batch 2 case 06 diagnostic | `f2bfcd81c6bf270a8cc08cfdf655d8c2740e5b4451b23a43dbd9436f4edbd2db` |
| `old-navigation-replay/report.json` | Readable old-code diagnostic | `34f174435ee0bfb9c358108c9893a3d32d50ba40d577f08f56bfe6d44827ebde` |
| `final-step-939be0ba/full-report.json.xz` | Lossless final 240-run step comparison; check failed | `f0a0da1de9836a02910134b71ef8bbcdda2413c94acd5268401a2be7f534c3f0` |
| `final-step-939be0ba/report.json` | Readable step evidence with all 240 per-run summaries | `1b76e37d0b6be32396ebb04371b65a7bf909818270ef80c9da0e1a97284398b7` |
| `final-teleport-939be0ba/full-report.json.xz` | Lossless final 24-run teleport regression; check failed | `5ebb175d416afd6a7e05b95e423e8109479e462dcc19c24c1f932b6675348331` |
| `final-teleport-939be0ba/report.json` | Readable teleport evidence with all 24 per-run summaries | `e714c7adb4698a03deea797b5ff719506be4f1d69b87eeab508cc6250d21e831` |

To verify a lossless archive against the original payload hash recorded inside its
adjacent `report.json`:

```bash
/home/dwb/.pyenv/bin/pyenv exec python - <<'PY'
import hashlib, json, lzma
from pathlib import Path

directory = Path("reports/executor_batch_3/evidence/full-baseline")
metadata = json.loads((directory / "report.json").read_text())
digest = hashlib.sha256(lzma.open(directory / "full-report.json.xz", "rb").read()).hexdigest()
assert digest == metadata["original_report_sha256"]
print(digest)
PY
```

The `old-navigation-replay` files are diagnostic only. The two smoke directories are
24-run checks. The final step and teleport workloads are complete, and both have
`check_passed=false`; their presence is evidence of completed execution, not passed
real acceptance.
