# Tokenizer dependency submission audit

This candidate builds on `d7cc9343ad21c39e53212e9b2b761489f67b6dfe`.
The existing coverage map remains byte-identical; earlier submitted commits
remain in history. The new profile is 22,884 bytes compressed.

## Measured improvement

| Version | Simulated seconds | Three-run Docker median | Missed commits |
| --- | ---: | ---: | ---: |
| Submitted `d7cc934` | 163.788 | 167.1 | 0 |
| Tokenizer dependencies | 136.239 | 139.5 | 0 |

The exact simulated reduction is 27.549 seconds (16.82%).
The five-run candidate median is 139.3 seconds.
All eleven before/after sample runs are valid and retain all 73 known failing
occurrences. Candidate output is identical across its eight runs.
The supplied scorer agrees with independent integer-millisecond accounting.

Candidate busiest-worker loads total 70.239 seconds;
33 occupied workers add 66.000 seconds.
Miss penalties are zero. Each missed commit would still cost 300 seconds.
CPU includes selector shutdown and a small measurement parent. Queue wait is
not part of the objective. Lower scores are better.

These measurements use the supplied 30-sample batch. The user-provided official
score of 328.4 seconds for `d7cc934` used 325.703 simulated seconds and about
2.69 CPU seconds, with zero misses. The different simulated workload prevents
predicting the new official score by subtracting this local improvement.

## Dependency contract

`build_tokenizer_map.py` discovers direct class-field arguments passed into
`TokenizerCore`. Each supported parameter must appear only in its plain slot
assignment. Baseline instrumentation follows defining-class ownership through
inheritance and retains the provenance of cached core instances across contexts.
The final profile supports 47 class attributes across 11 field names.
It reads no sample patches, durations, answers or mutant results.

The runtime requires unchanged assignment targets, annotations, literal types,
container structure and ordered unique dictionary keys. `NUMERIC_ESCAPES` values
may change while those keys stay fixed. The pinned import-time escape-format
derivation observes only key membership, so its generated format stays unchanged.
Value reads or unknown collection consumers retain the fallback. Namespace,
frame, global/nonlocal and reflective escapes invalidate that key-only proof.

Direct, builtin and supported standard-library reflection are recorded, including
aliases and keyword calls. Cached inherited core readers remain linked to their
defining class; unknown or copied cores retain their whole contexts. Source-file
readers, metadata readers, class constructors and incomplete/unknown tests remain
selected. Shared fixture setup and teardown dependencies are propagated.
The earlier class-schema, callable, factory, comparison and subprocess evidence
is unchanged. Duplicate AST parsing/comparison work is removed without changing
the comparison proof. Signature/default changes remain conservative.

The profile stays separate from `coverage-map.json.gz` to keep repository history
small. Missing, corrupt, stale or incomplete evidence retains the existing
fallback. Runtime uses only Python's standard library and never executes a patch.

## Correctness evidence

The unchanged baseline and the final instrumented baseline each pass 1,620 tests
and 25,440 subtests, with one documented optional SQLite skip. Exact node IDs and
source Git blobs match the pinned commit. Final instrumentation takes
236.920 seconds as an offline preparation step.
Its metadata records builder/helper hashes. There are no collection errors,
background-thread reads, opaque reflection contexts or unknown-core contexts.

All 217 regression tests pass in pinned Python 3.13 Docker, without
skipped regression tests. Independent miniature suites exercise inherited fields,
cached/copied cores, reflective escapes, static reflection, keyword arguments,
dynamic aliases, global/nonlocal escapes and conservative rejection paths.

Twelve new mutant selections were frozen before execution. Eleven edits avoid
sample-edited lines; the explicitly labeled Snowflake numeric regression changes
a different tuple entry on the same sampled source line. Four mutants execute
the full suite. Five cases execute every narrowed selection plus dialect,
tokenizer, generator, serialization and fixed unrelated checks. Two structural
changes retain full-suite selections but execute only targeted validation subsets
(113 and 120 collected tests); they do not execute every selected test. One
invalid configuration attempts full-suite collection. There are 4 actual
failing-test occurrences and 1 collection error affecting
42 manifest tests. Collection failures are accounted for
separately from executed test failures; the invalid configuration retains the
full suite. No observed failure is omitted.

| Mutation | Scope | Observed failures | Collection errors | Selected | Omissions |
| --- | --- | ---: | ---: | ---: | ---: |
| `postgres_hex_base` | full suite | 1 | 0 | 226 | 0 |
| `hive_unicode_base` | full suite | 0 | 0 | 246 | 0 |
| `bigquery_octal_width` | selected + fixed checks | 0 | 0 | 226 | 0 |
| `clickhouse_hex_limit` | selected + fixed checks | 0 | 0 | 110 | 0 |
| `duckdb_byte_semantics` | selected + fixed checks | 0 | 0 | 251 | 0 |
| `hive_surrogate_replacement` | selected + fixed checks | 0 | 0 | 246 | 0 |
| `snowflake_unknown_escape` | full suite | 0 | 0 | 275 | 0 |
| `materialize_unicode_base` | selected + fixed checks | 0 | 0 | 13 | 0 |
| `bigquery_changed_key` | targeted checks; full fallback | 3 | 0 | 1620 | 0 |
| `clickhouse_bad_map_type` | collection failure; full fallback | 0 | 1 | 1620 | 0 |
| `hive_bad_tuple_shape` | targeted checks; full fallback | 0 | 0 | 1620 | 0 |
| `snowflake_hex_base_targeted` | full suite | 0 | 0 | 275 | 0 |

The final profile produces the same selections that were frozen before execution.
The final limited Docker image independently produces those same choices and
passes the supplied output rules against the observed outcomes. The 34 older
mutation cases also retain all 686 recorded failing-test occurrences; that step
rechecks selections rather than rerunning all older mutants.

This is evidence of zero observed misses, not a universal proof for hidden
patches or arbitrary reflection and persistent cross-test data flows. No global
optimality or hidden-set score is claimed. More aggressive constructor removal
and default-argument narrowing were left out because their evidence is weaker.

## Submission constraints

The root Dockerfile uses the exact pinned linux/amd64 Python digest, copies only
runtime modules plus the two baseline artifacts, and runs as user 65534.
A fresh no-cache build succeeded in 15.078 seconds
under verified four-CPU/eight-GiB builder limits and the 600-second timeout.
Offline instrumentation is not part of the submission build.

Runtime checks verify two CPUs, 4 GiB RAM, 256 processes, no network, read-only
root, HOME=/tmp and initially empty 64 MiB tmpfs. Peak measured RSS is
268.29 MiB; maximum measured sample wall time is 5.312 seconds.
Stdout is 3,916 bytes of valid UTF-8 JSONL, below 2 MB.
Every commit has exactly one answer, tests are never duplicated, and occupied
workers never exceed eight. Logs use stderr. Samples, answers, mutant results
and builders are absent from the image. Image file hashes match the worktree.

A 45-commit repeated-sample capacity check validates the larger-batch runtime;
it is not hidden-final correctness evidence. Repository size is checked including
retained Git objects and current files. There are no LFS pointers or submodules.
No contest-platform submission is performed by these validation tools.

## Evidence

- [Before](tokenizer-before.json), [three runs](tokenizer-after-three.json), [five runs](tokenizer-after-five.json).
- [New mutation summary](tokenizer-mutation-validation.json), [final image mutation audit](tokenizer-image-mutations.json).
- [Prior mutation recheck](tokenizer-prior-correctness.json), [runtime/build/size audit](tokenizer-runtime-audit.json).
- [Capacity check](tokenizer-capacity.json), [previous class-schema audit](BASELINE_D7CC934_AUDIT.md).
