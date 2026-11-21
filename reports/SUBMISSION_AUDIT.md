# Scoring optimization audit — 21 November 2026

The previously submitted commit `f659c6224811783325de7701c225b783427e6eed`
remains in history. The user supplied its official result: 367.501 simulated
seconds, roughly 2.43 CPU seconds, and **zero missed commits in all three runs**.
That official 369.9-second score used a different batch from the local samples.
The new implementation has not been submitted to the contest platform by this
agent. Its official score and rank require a new submission.

## Exact local scoring

| Component, all 30 sample commits | Seconds |
| --- | ---: |
| Busiest-worker loads | 122.146 |
| 38 nonempty workers × 2 seconds | 76.000 |
| 0 missed commits × 300 seconds | 0.000 |
| Simulated total | 198.146 |
| Three-run median, including measured process CPU | **201.5** |
| Five-run median, including measured process CPU | **201.5** |
| Previous local three-run median | 290.6 |

The three-run improvement is 30.7%. All eight outputs are identical;
all 73 sample failing-test occurrences are selected. Arithmetic uses integer
milliseconds and final median rounding uses integer microseconds. The untouched
supplied scorer agrees with the independent accounting on every run. CPU
includes full selector teardown and a small measurement parent; official server
CPU may differ. Worker startup cost is normal cost, not a missed-commit penalty.

## What changed and why

- AST equality avoids repeatedly constructing large formatted strings; parsed
  trees are reused and empty decorator scans are skipped.
- Baseline Python child tracing replaces a blanket uncertain-test inclusion with
  whole-file dependencies. Unsupported children retain uncertainty.
- Exact collection call events allow single-line dispatch lambda refinement.
  Nested structural edits retain their enclosing control-flow boundary.
- Inert forwarding-only closure factories with immutable captures retain their
  callers, class consumers and attribute readers. Unknown inheritance/hooks,
  persistent effects and import-time invocation retain broad selection.
- Raw operand observations support site-wide comparison equivalence. Every
  observed result must stay the same under a proven pure comparison. The rule
  cannot override existing unsafe/import/decorator fallbacks, cannot cross other
  edited files, and preserves source, reflection and uncertain-test dependencies.
  Mixed opaque/value comparisons remain unknown because of reflected equality.

Independent review found and fixed cross-test cache contamination and reflected
equality counterexamples before enabling comparison pruning. It also fixed the
recorder's handling of unsupported replay so instrumentation cannot replace the
original comparison result with a recorder exception.

## Correctness evidence

All **174 regression tests pass** in the pinned Python 3.13 Docker image. The
original full baseline and new instrumented baseline each passed 1,620 tests and
25,440 subtests, with one expected SQLite skip. New preparation checks verify all
source Git blobs and the exact baseline node-ID list.

| Independent validation | Mutations | Mutant test executions | Observed failures | Omitted |
| --- | ---: | ---: | ---: | ---: |
| Original adversarial subset, rerun | 15 | 270 | 53 | 0 |
| Structural and lambda cases | 4 | 3,898 | 622 | 0 |
| Closure capture cases, full suite | 2 | 3,240 | 1 | 0 |
| Comparison equivalence and changed-behavior cases | 10 | 140 | 4 | 0 |

Choices were frozen before mutant execution. Raw mutant patches and failure
lists stay outside the submission repository and runtime. Aggregate reports are
linked below. The 38 executed tests actually pruned in the comparison validation
all passed. These tests do not establish universal hidden-set soundness; unknown
reflection, cross-module state and environment behavior remain heuristic limits.

## Submission and resource rules

- Root Dockerfile uses the starter's exact pinned Linux/amd64 Python image and
  copies committed files. Build downloads are only the permitted Docker Hub base.
- A fresh isolated BuildKit worker had its actual 4-core/8-GiB settings inspected.
  The no-cache build completed in **14.203 seconds**, below the 600-second limit.
- Operand-map preparation took **647.631 seconds offline**. It is not part of
  the submission build; the generated map is committed and copied into the image.
- Runtime limits were inspected: 2 CPU cores, 4 GiB, 256 processes, UID/GID 65534,
  HOME `/tmp`, read-only root, initially empty 64-MiB tmpfs, only loopback networking.
- Sample peak RSS was 277.88 MiB. The 45-commit capacity check took
  6.515 seconds and 283.93 MiB,
  below the final 270-second/4-GiB limits. That batch repeats samples and measures
  capacity only, not correctness on unseen final data.
- Output is valid UTF-8 JSONL, one line per commit, no duplicate/unknown tests,
  at most eight occupied workers and 16,711 bytes on the samples,
  below 2 MB. Logs go to stderr. Unknown patches/maps retain a safe broad fallback.
- Runtime contents were inspected: no sample answers, samples, mutant labels,
  network calls or patch fingerprints. Maps contain only pinned-baseline source,
  coverage, imports/readers, class dependencies and raw operand observations.
  Current test numbers are joined through node IDs, not hardcoded.
- The public repository keeps prior submitted commits. Its final recorded size
  is below 20 MB and it uses neither gitlinks nor Git LFS. Administrative team
  membership, submission quotas and final chosen commit remain platform state.

## Evidence

- [Three runs](sample-validation.json) and [five runs](sample-validation-five-runs.json)
- [Runtime/build inspection](runtime-audit.json) and [capacity](capacity-validation.json)
- [Baseline provenance](baseline-validation.json)
- [Adversarial subset](heldout-summary.json)
- [Precision mutations](precision-validation-summary.json)
- [Factory mutations](factory-validation-summary.json)
- [Comparison mutations](comparison-validation-summary.json)
