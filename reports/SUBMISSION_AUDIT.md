# Submission re-verification — 21 November 2026

This audit covers the selector, map and reports in this commit. The prior public
commit `3f73e5933e8a3f34fccecba5475258457697a695` remains in repository history.
No official contest submission was made by this audit.

## Exact penalty accounting

The supplied `score.py` is unchanged from the starter. A separate implementation
in `scoring_audit.py` independently calculates each component and asserts exact
agreement with it on every benchmark run.

| Component, all 30 sample commits | Seconds |
| --- | ---: |
| Sum of busiest-worker loads | 202.689 |
| 43 occupied workers × 2 seconds | 86.000 |
| 0 missed commits × 300 seconds | 0.000 |
| Simulated total | **288.689** |
| CPU in median three-run result | 1.923199 |
| Median total before rounding | 290.612199 |
| Half-up rounded three-run result | **290.6** |
| Half-up rounded five-run result | **290.5** |

A commit incurs one 300-second penalty if *any* failing test is omitted, even if
several failures are omitted. Regression tests verify that case, penalties on
multiple commits, empty workers, numeric-ID lookup, worker maxima, median
selection and half-tenth rounding. Final totals use integer microseconds, avoiding
the floating-point boundary error discovered in the earlier benchmark.

All 73 failing test occurrences in the sample answers are selected. All eight
measured runs are valid and produce identical output. CPU includes the selector's
complete process lifetime plus a small measurement parent; the parent's final
few instructions are outside its last resource read. Official server CPU and
hidden-set results can differ. The simulated total is exact for these outputs.

## Technical rules

| Requirement | Verification |
| --- | --- |
| Public, team-owned GitHub repository and root Dockerfile | Repository is public under the authenticated `kundansai-138` account; root Dockerfile and submitted history retained. |
| Each patch independent against the pinned commit | Full baseline commit verified; each patch applies in memory to original source, never another patch's result. |
| Correct input files and current test numbering | Reads `/data/commits` and `/data/tests.json`; joins baseline node IDs to current numbers. Unknown tests are retained. |
| Exactly one JSON line per commit; logs only stderr | Official parser and independent checker validate all lines, complete IDs, exact object keys and UTF-8 output. |
| At most 8 workers; no duplicates; valid test numbers | Validated by the supplied scorer and scheduler regression tests. Empty workers do not accrue cost. |
| Stdout below 2 MB | 24,000 bytes per sample batch. The 45-commit capacity check produced 33,701 bytes. |
| Build: 4 cores, 8 GB, at most 10 minutes | Fresh isolated BuildKit worker's actual CPU quota and 8 GiB memory limit inspected. Initial cold build 32.6 s; final no-cache build 4.2 s. |
| Permitted build downloads only | Submission Dockerfile downloads only the starter's pinned Python Docker Hub base, then copies committed files. It performs no package installation or source fetch. |
| Maps derived only from pinned baseline | Source Git blobs verified against full pinned commit. Full baseline trace passed 1,620 tests and 25,440 subtests, with the expected SQLite skip. New static indexes derive only from those unchanged sources and traces. |
| No answer or edit-kind lookup | Runtime code has no sample-answer, patch-ID or edit-category failure table. Actual image contents inspected; sample answers are absent. Sample labels serve validation only. |
| Repository below 20 MB; no LFS/submodules | Tracked artifacts and retained Git history are below the limit. Map is an ordinary committed gzip file; no gitlinks or LFS objects are used. |
| Linux/amd64 | Image architecture and OS inspected. The exact starter image digest is preserved. |
| Runtime: 2 cores, 4 GB, 256 processes | Actual cgroup values inspected: CPU `200000 100000`, memory 4,294,967,296 bytes, pids 256. |
| Nobody, HOME=/tmp | Actual UID/GID 65534 and HOME `/tmp` inspected. |
| No network | `--network none`; actual container has only loopback. Runtime contains no network calls. |
| Read-only disk, initially empty 64 MB /tmp | Root read-only flag, empty `/tmp`, and 67,108,864-byte tmpfs verified inside container. Bytecode writes disabled. |
| Runtime wall limits | Largest measured sample wall time 3.14 s versus 180 s. A 45-commit capacity check took 3.44 s versus the final 270 s allowance. |
| Failures include errors, import errors and subtests | Baseline recorder and mutation harness preserve error/subtest outcomes; import-failure and serialization regressions tested. |
| Comments are not automatically harmless | Whole-file AST comparisons, source readers and uncertain tests are retained; every file in mixed patches is examined. SQL fixture comments are treated as data. |
| Three-run median and final five-run median | Both measured locally; integer-microsecond half-up rounding tested. Ties/earlier submission ordering are platform behavior, not controlled by this repository. |

The 45-commit capacity check repeats sample patches under new IDs. It measures
batch capacity, **not** correctness on unseen final patches.

## Correctness fixes and optimization

The review found concrete failures beyond the original samples: cached dispatch
initialization could affect later tests without executing its producer again;
class fields could be read without construction; `__slots__` changes could fail
imports in built-in class machinery; and decorators could observe unexecuted
function code. Added safeguards cover those cases and newly introduced shared
state. Recursive and aliased mutation analysis has dedicated regressions.

Optimization narrows stable statements and eligible direct dispatch lambdas
using baseline coverage. It preserves broad fallbacks for changed bindings,
comprehensions, new control flow, import execution, persistent state and uncertain
cases. This reduced simulated sample time by **40.266 seconds** from the prior
328.955-second result while retaining every known failure.

All **97 regression tests pass in the pinned Docker environment**. Independent
validation executed 15 deterministic mutations outside sample-edited lines,
using 18 predetermined tests per mutation and freezing decisions before running
the mutations. Thirteen mutations produced 53 failure observations; none was
omitted. Final selections were rechecked against those observations after the
last conservative changes. Only aggregate results are published; no generated
mutation-to-failure lookup is present in the runtime or dependency map.

This remains a baseline dependency heuristic. The fixed-subset mutation checks
and sample success do not prove correctness for every possible hidden patch,
particularly arbitrary reflection, cross-module state or untraced native code.

## Platform and administrative conditions

The repository can establish code provenance, public visibility, retained
commits and technical compliance. It cannot establish whether a person has
another team, outside code-sharing history, the event's submission quota,
official chosen commit, cutoff enforcement or future public availability.
The user must submit the reported GitHub URL and commit on the contest platform,
observe its quota and team rules, and keep submitted commits public through
publication of results. No leaderboard ranking or prize claim is made.

## Evidence

- [Three-run scoring report](sample-validation.json)
- [Five-run scoring report](sample-validation-five-runs.json)
- [Actual runtime and build limits](runtime-audit.json)
- [Baseline map provenance](baseline-validation.json)
- [Independent mutation aggregate](heldout-summary.json)
- [45-commit capacity check](capacity-validation.json)
