# Integrated submission audit

This change builds on `205e093b3feb38913cb4313a252aa456d25513b2`. The earlier
submitted commits remain in Git history. It adds class-qualified schema
dependencies without replacing existing optimizations or map data.

## Fresh local score

| Version | Simulated seconds | Three-run Docker median | Missed commits |
| --- | ---: | ---: | ---: |
| Before (`205e093`) | 198.146 | 199.9 | 0 |
| Integrated class dependencies | 163.788 | 165.9 | 0 |

All six comparison runs were valid and retained all 73 known failing-test
occurrences. Each version produced deterministic output. The supplied scorer
and independent integer-millisecond accounting agree. The integrated cost is
93.788 seconds of busiest-worker loads plus 70.000 seconds for 35 workers, with
zero 300-second miss penalties. Median rounding follows the half-up rule.

The improvement is 34.358 simulated seconds (17.34%). Only the targeted
`TriggerProperties.arg_types` sample changes: 1,620 tests / four workers /
36.817 seconds becomes seven tests / one worker / 2.459 seconds. The other
29 sample selections are unchanged. These local numbers do not establish a new
official hidden-set score; the previously supplied 369.9-second official result
used the server's batch. No contest-platform submission is performed here.

## Correctness and preservation

- All existing map entries are equal to the pre-integration map after removing
  the new `class_attribute_profile` entry. Unrelated Python modules are unchanged.
  Callable, factory, comparison, subprocess, AST and scheduling optimizations
  therefore remain present.
- The integrated runtime modules and map match the validated isolated candidate
  byte for byte. Image contents were independently hashed against the worktree.
- A fresh regression run passed **184 tests** in pinned Python 3.13 Docker, with
  no skipped regression tests.
- Integrated selections were rechecked against **34 previously executed mutation
  cases**, retaining all **686 recorded failing-test occurrences**. This pass
  reselected the tests; it did not repeat the previous full mutant-suite runs.
  Prior selections were frozen before those actual executions.
- The three class-schema cases previously ran the entire baseline suite and
  produced 3, 0 and 3 failing tests respectively. The merged Docker image still
  selects the same seven tests and misses none of those failures.
- The final class-profile builder ran the complete unchanged baseline:
  1,620 tests and 25,440 subtests passed, with one expected environment skip.
  It verified source Git blobs and exact test IDs and found no collection errors,
  opaque reflection contexts or background-thread reads.

This remains a baseline dependency heuristic. The observations do not prove
universal correctness for hidden mutations, arbitrary native reflection or
persistent cross-test data flows. Known unsupported cases keep the conservative
fallback; the schema profile applies only to its explicit boolean-value contract.

## Runtime and submission constraints

The root Dockerfile still uses the starter's pinned linux/amd64 Python base and
copies committed runtime files and the baseline map. A fresh no-cache local
build succeeded. It adds no package download or map-generation step to submission
builds. The separately reproducible map builder is not part of scored execution.

Fresh runs used two CPUs, 4 GiB memory, a 256-process limit, user 65534, HOME
`/tmp`, no network, a read-only root and an initially empty 64 MiB tmpfs.
Peak RSS was 271.83 MiB and the largest measured wall time was 2.937
seconds, including Docker startup. Stdout was 9,746 bytes of valid JSONL, below
2 MB. Every commit has one line, no test is duplicated, and occupied workers
never exceed eight. Logs go to stderr.

Image file hashes confirm that samples, answers, mutation labels, builders and
validation reports are absent from `/app`. The map records baseline dependencies,
not patch fingerprints or lookup tables of failures. Test numbers are joined
through node IDs. The public repository retains the previous submitted history.

## Evidence

- [Before](class-integration-before.json) and [after](class-integration-after.json): fresh three-run Docker measurements.
- [Correctness and runtime hashes](class-integration-correctness.json): integration preservation and mutation rechecks.
- [Class dependency design](CLASS_DEPENDENCY_AUDIT.md): contract, workflow and fallback behavior.
- [Previous optimization audit](BASELINE_205E093_AUDIT.md): historical measurements and earlier mutation execution scopes.
- [Baseline provenance](baseline-validation.json): pinned source and environment evidence.
