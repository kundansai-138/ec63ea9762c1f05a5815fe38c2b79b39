# Blast Radius: select from baseline dependencies, then run the candidates

This submission answers each commit in two stages.

1. **Select.** Dependencies measured on the unmodified commit
   `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` name the tests a change can
   reach. Uncertain changes widen the selection, up to the complete suite.
2. **Run.** When the candidates are cheap, the selector applies the patch to a
   copy of the pinned sqlglot checkout inside the container and runs those
   tests. A candidate is dropped only if it ran to completion and passed.
   Everything else stays selected, then the worker cost is minimized.

Stage 2 is what removes most of the cost: a change that reaches a handful of
tests but breaks none of them is answered with no workers at all, instead of
two seconds for a worker that finds nothing.

The image holds the pinned sqlglot checkout and the test dependencies at the
versions in `requirements.lock`. At run time it reads `/data/tests.json` and
`/data/commits/*.patch`, writes one compact JSON answer per commit to stdout,
and sends diagnostics to stderr. It needs no network.

## Sample result

Measured locally on the 30 sample commits, in the built image under the
scorer's limits (2 CPUs, 4 GB, no network, read-only disk, user `nobody`):

| Version | Simulated seconds | Missed commits |
| --- | ---: | ---: |
| Baseline dependencies only (`d7cc934`) | 163.788 | 0 |
| With module scope and candidate runs | 119.139 | 0 |

The best possible answer for these 30 commits costs 94.4 simulated seconds.

CPU seconds are charged on top. On the development machine one run used about
8.4 CPU seconds, but that machine runs sqlglot's tests about 3.4 times slower
than the recorded durations in `tests.json`, so this number does not transfer.
These are **local sample scores, not official leaderboard results**.

Evidence that running candidates agrees with the labeling method: on 146
synthetic patches, the 1,205 test verdicts produced by `runner.py` were
compared with real pytest in the locked environment. 1,204 agreed. The one
disagreement was a test the runner failed and pytest passed, which costs time
but cannot cause a miss. There was no case of the runner passing a test that
pytest failed. This is evidence, not proof, for hidden patches.

All 184 regression tests in `tests/` pass. The reports under `reports/`
describe the earlier baseline-only versions.

## Selection

`build_map.py` runs the pinned baseline tests to record dependencies and builds
static indexes through `enrich_map.py`. The latter can also refresh indexes from
an existing baseline map without executing any proposed changes. The map
contains baseline source text, per-test line coverage, files read during tests,
and class construction dependencies. Map identities are pytest node IDs, never
the sample manifest's numeric test IDs. At runtime the selector joins node IDs
to the current manifest; unfamiliar tests and baseline tests with incomplete
execution are always included.

The selector applies each unified diff in memory and verifies its original
context. For supported Python edits it parses and compiles the resulting source,
then compares the old and new ASTs:

- Existing function-body changes use whole-statement coverage when lexical
  bindings remain unchanged. Changed conditions retain their full compound
  statement. Structural changes within an unchanged compound retain its entire
  interval; top-level structural changes, comprehension changes and scope changes
  retain whole-function coverage.
- Direct dispatch lambdas use baseline call events to distinguish import-time
  construction from body execution, including single-line lambdas. Without that
  evidence, a mandatory interior bytecode line must establish nonexecution.
  Dictionary keys, defaults, signatures and call targets remain unchanged;
  added mutation, introspection and persistent-state calls retain the fallback.
- A literal argument change to an inert closure factory can retain closure-body
  callers, class consumers and attribute readers. The factory must only define
  and return the same undecorated closure; its body must not run during collection.
  Recursive base-class checks reject unknown or custom construction hooks.
- Function metadata readers are retained even when they do not call the changed
  function. Changed docstrings also preserve the conservative fallback.
- For an isolated equality/identity operator substitution in an ordinary function,
  raw baseline operands must establish equivalence for every observation at the
  entire site. A divergent or unknown observation disables the whole refinement;
  it cannot override a persistent-state or import-time fallback. Replay supports
  proven builtin comparison slots and preserves identity, numeric types, NaN and
  signed zero. Mixed opaque-identity/value comparisons remain unknown because of
  reflected equality. Source and reflective metadata readers remain selected.
  The map stores baseline operands and contexts; it contains no precomputed
  outcomes for proposed operator edits.
- Existing or newly introduced persistent state, cached result producers,
  unrecognized decorators, and import-time function execution fall back to the
  full suite. Static analysis includes global/class state, aliases, mutable
  defaults, recursive helper calls, and transitive same-module callees.
- Literal assignments inside supported classes select tests that constructed
  the class or its subclasses. Constructor instrumentation records each class's
  MRO for sqlglot expressions, parsers, tokenizers, generators, and dialects.
  Tests reading the same attribute without constructing the class are also
  included. Missing class evidence falls back to module consumers, or the full
  suite. Dunder assignments such as `__slots__` always retain the full suite,
  because built-in class construction can fail outside Python tracing.
  Attribute names used by baseline statements executed during test collection
  are retained separately. Changing a class attribute with one of these names
  falls back to the full suite because its effects may have occurred at import
  time, before per-test constructor tracing begins.
- A class-qualified schema profile refines the collection fallback for
  `arg_types` when unchanged ordered, unique string keys retain boolean values.
  It records readers of the exact defining class and import-time derived
  `required_args` sets, including inherited fields. Constructors, source-file
  readers, reflective readers and uncertain tests remain selected. Unknown
  collection consumers, escaped collection reflection, custom metaclasses,
  class-local aliases, changed keys and non-boolean values retain the fallback.
  The profile is derived only from the unchanged pinned baseline, without
  patch IDs, failure labels or precomputed outcomes for proposed changes.
- An AST-preserving edit still selects tests that read the source file as data.
  Comment-shaped encoding declarations are parsed rather than assumed harmless.
- Changes to fixtures and other non-Python files select their recorded readers.
  Missing reader evidence falls back to the full suite.
- Changed signatures, decorators, imports, unsupported top-level expressions,
  test code and syntax errors fall back to the **module scope** of the edited
  file (next section). Unknown files, unsupported patch operations, and invalid
  or missing maps fall back to the full suite.

### Module scope

`build_isolation_map.py` runs every baseline test alone, in a fresh process,
and records the files whose module-level code that process executes. A change
confined to one existing file cannot affect a test whose own process never
imports the file, so the importers of the file bound what the change can break.
sqlglot loads dialect modules lazily, which makes this bound far smaller than
the suite for dialect, parser, generator and typing modules. Tests that read
the file as data, and tests without usable evidence, are always added. For a
file every test imports, the bound is the complete suite, as before.

Running each test alone matters: in a shared process the first test to need a
module or a cached value pays for it, and later tests reuse it invisibly.

### Running the candidates

`verification_plan.py` decides, per commit, whether running candidates is worth
its CPU. It compares the simulated cost of scheduling them with the predicted
CPU of running them, taken from `verify-profile.json` (each baseline test's
measured in-process cost). Readers of an edited fixture are assumed to fail and
are not run. The decision never looks at what the patch contains.

`verify.py` forks one worker per commit. The worker builds the patched tree
under `/tmp` (symbolic links to the pinned checkout, real files only where the
patch changed something) and `runner.py` runs the tests with `unittest`, in
collection order, with `SKIP_INTEGRATION=0`. A test counts as failing on a
failure, an error, a failing subtest, an unexpected success, a failed class or
module fixture, a CPU-time overrun, or a test file that cannot be imported. A
worker that dies or hangs is killed and restarted for the remaining tests; a
test it never judged stays selected. Verification stops 150 seconds after
start and everything unverified keeps its baseline selection.

The executor and optimizer tests hand work to a spawned process pool. The
runner executes those jobs in-process with a pickle round trip, as the map
builder already did, so that the work runs on the patched tree and is charged
once.

The builder propagates shared fixture setup to all tests in its class, module,
package, or session. Teardown dependencies are conservatively attributed to the
whole module. Baseline optimizer and executor `ProcessPoolExecutor` jobs run
inline while recording, with real multiprocessing pickle round trips for
arguments, results, initializers, and exceptions. This captures serialization
dependencies as well as child computations and fixture reads. The separate
`build_subprocess_map.py` traces supported Python children on the unchanged
baseline and adds whole-file dependencies for imported, executed and opened
files. It removes uncertainty only after a passing test and complete child exit
reports. Unsupported or untraced subprocesses remain conservatively included.
`SKIP_INTEGRATION=0` enables the executor tests.

This is a baseline dependency heuristic, not proof that every hidden failure is
covered. Reflection, import-time behavior, environment differences, and dynamic
dependencies can evade recorded evidence. Conservative fallbacks reduce that
risk without making an unsupported soundness claim.

## Scheduling

`scheduling.py` considers one through eight workers. It uses the scorer's integer
millisecond durations and adds 2,000 milliseconds per occupied worker. Each
candidate begins with longest-processing-time-first assignment; bounded moves
and swaps improve uneven loads. A lower bound from the longest indivisible test
and total work avoids computing candidates that cannot beat the current best.
Ties retain the smaller worker count. Empty selections use no workers.

Selections with the same test set share a cached schedule within a batch.
Scheduling changes allocation only: every selected test appears exactly once.

## Run and validate

From the repository root, these commands also work in PowerShell without make:

```sh
docker build --platform linux/amd64 -t blast-radius-entry .
python benchmark.py --image blast-radius-entry --runs 3 --report work/docker-benchmark.json
python benchmark.py --image blast-radius-entry --runs 5 --report work/docker-benchmark-five.json
```

In a shell with Docker, make, and Python available:

```sh
make sample
make check
python -m unittest discover -s tests -v
python benchmark.py --image blast-radius-entry --runs 3 --report work/docker-benchmark.json
```

`make sample` builds the top-level Dockerfile, runs the selector with the
competition's restrictions, and invokes the supplied scorer. `make check` also
invokes that scorer; its success checks output validity, not whether the
selection missed a failing test.

For a local development check without Docker, in a POSIX shell:

```sh
python selector.py --data sample > output.jsonl
python score.py sample sample/answers.json output.jsonl
python benchmark.py --runs 3 --report work/local-benchmark.json
```

The local scorer reports simulated CI seconds and sample misses. Its output
does not measure container CPU or reproduce the official median-of-three
score. The final hidden batch can behave differently from the samples.
Linux/amd64 Docker execution under the provided limits is the final validation
step; a successful host-Python run alone does not establish that validation.
`benchmark.py --image ...` measures fresh container runs under those limits and
includes the selector's process CPU plus a small measurement parent. It reports
each run, the median total, misses, and output determinism. Without `--image`,
the report is explicitly a host-Python measurement and excludes interpreter
teardown from its CPU figure.

## What the image build does

The build installs nine pinned packages from PyPI (the ones sqlglot's tests
import, at the versions in `requirements.lock`), downloads sqlglot at the
pinned commit from its own GitHub repository, checks every text file against
the baseline sources in the committed map, and compiles bytecode. It does not
run sqlglot's tests. `prepare_runtime.py` stops the build if any source file
differs from the map, because patches are applied to the map's copy.

## Rebuild the isolation map and the cost profile

Both are built from the pinned commit only, inside the submission image (it
holds the checkout and the locked test dependencies):

```sh
docker run --rm --user 0 -v "$PWD:/repo:ro" -v "$PWD/work:/out" blast-radius-entry \
  bash -c 'cd /repo && python build_isolation_map.py --source /opt/sqlglot \
    --tests sample/tests.json --output /out/isolation-map.json.gz --jobs 8'
docker run --rm --user 0 -v "$PWD:/repo:ro" -v "$PWD/work:/out" blast-radius-entry \
  bash -c 'cd /repo && python build_verify_profile.py --source /opt/sqlglot \
    --tests sample/tests.json --output /out/verify-profile.json'
```

Neither file contains a patch, a failure label or the outcome of any proposed
change. All 1,620 tests pass when run alone, so no test lacks evidence.

## Rebuild the dependency map

Normal submission builds copy the already committed map and do not run sqlglot's
tests. Map generation is a separate, more expensive preparation step. For a map
matching the scorer environment, use Linux/amd64 and the starter's pinned base:

```text
python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
```

Create a clean checkout at exactly the pinned commit, if one is not already
available. This preparation step runs on the host with Git available:

```sh
git init /tmp/sqlglot-baseline
git -C /tmp/sqlglot-baseline remote add origin https://github.com/tobymao/sqlglot.git
git -C /tmp/sqlglot-baseline fetch --depth 1 origin eb11aec4be8cb2122afe713a4816662c3f0fb2fa
git -C /tmp/sqlglot-baseline checkout --detach FETCH_HEAD
```

From this repository, export that checkout and build the separate map-generation
image. The following commands use a POSIX shell; change the source path to the
location of your clean checkout:

```sh
python prepare_baseline.py --source /tmp/sqlglot-baseline --output-dir work/map-container
docker build --platform linux/amd64 -f Dockerfile.map -t blast-radius-map-builder .
mkdir -p work/map-results
docker run --rm --platform linux/amd64 --network none --cpus 4 --memory 8g \
  --mount "type=bind,source=$PWD/work/map-container,target=/input,readonly" \
  --mount "type=bind,source=$PWD/work/map-results,target=/output" \
  blast-radius-map-builder python -c '
import subprocess, sys, tarfile
with tarfile.open("/input/baseline.tar") as archive:
    archive.extractall("/tmp/sqlglot", filter="data")
raise SystemExit(subprocess.call([
    sys.executable, "/app/build_map.py", "--source", "/tmp/sqlglot",
    "--source-manifest", "/input/manifest.json",
    "--output", "/output/coverage-map.json.gz"
]))
'
```

`prepare_baseline.py` verifies the commit and tracked working-tree state, exports
the committed blobs with LF line endings, and checks every archive entry against
its Git blob hash. Its manifest records the full commit and per-file blob hashes,
including symbolic-link target bytes. The builder uses `--source-manifest` to
verify the extracted tree without requiring Git in the image.
`Dockerfile.map` installs the supplied `requirements.lock` plus
`coverage==7.13.5`; its downloads are from PyPI and the pinned Docker Hub base.
Map generation itself runs without network access.

After the initial map is generated, run these supplemental steps in the same
locked preparation image with the extracted baseline at `/tmp/sqlglot`, the
manifest at `/input/manifest.json`, and writable `/output`. Each command verifies
the pinned source; the comparison step executes the full unmodified test suite.
These preparation runs are separate from the scored submission build.

```sh
python /app/build_callable_map.py --source /tmp/sqlglot --source-manifest /input/manifest.json --map /output/coverage-map.json.gz --output /output/coverage-map.json.gz
python /app/build_subprocess_map.py --source /tmp/sqlglot --source-manifest /input/manifest.json --map /output/coverage-map.json.gz --output /output/coverage-map.json.gz
python /app/build_comparison_map.py --source /tmp/sqlglot --source-manifest /input/manifest.json --map /output/coverage-map.json.gz --output /output/comparison-map.json.gz
python /app/enrich_map.py --input-map /output/coverage-map.json.gz --output-map /output/coverage-map.json.gz --comparison-profile /output/comparison-map.json.gz
python /app/build_class_map.py --source /tmp/sqlglot --source-manifest /input/manifest.json --map /output/coverage-map.json.gz --output /output/class-map.json.gz
python /app/enrich_map.py --input-map /output/coverage-map.json.gz --output-map /output/coverage-map.json.gz --class-profile /output/class-map.json.gz
```

Do not use its development-only `--test` subset option for a final map. Inspect
the generated metadata for collection errors, failed/skipped tests, dependency
versions, platform, and whether a subset was used. Baseline failures are marked
incomplete and conservatively selected, but a complete successful build in the
locked environment is preferred. The generator exits with pytest's exit code;
investigate a nonzero result before replacing a validated map.

Inspect and score the candidate before replacing the committed map:

```sh
python -c 'import gzip,json; m=json.load(gzip.open("work/map-results/coverage-map.json.gz","rt")); print(json.dumps(m["metadata"],indent=2)); assert m["metadata"]["pytest_exit_code"] == 0; assert not m["metadata"]["collection_errors"]; assert m["metadata"]["test_subset"] is None'
python selector.py --map work/map-results/coverage-map.json.gz --data sample > work/candidate.jsonl
python score.py sample sample/answers.json work/candidate.jsonl
cp work/map-results/coverage-map.json.gz coverage-map.json.gz
make sample
```

To run the generator directly in an already prepared matching Linux environment:

```sh
python -m pip install -r requirements.lock coverage==7.13.5
python build_map.py --source /tmp/sqlglot-baseline --output work/candidate-map.json.gz
```

Commit the map as an ordinary file. No Git LFS or submodule is needed. The
submission image contains no sample answers, patch fingerprints, or mappings
from edit categories to failures. The builder never reads the sample patches
or answers; the sample answers are used only by the local scorer. The Docker
build context excludes those samples.

## Submit

Publish this repository under your own team's public GitHub account, keeping the
Dockerfile at its root. Submit the public repository URL and the exact commit
hash after running `make sample` successfully. Keep submitted commits available
until results are published. The supplied [SCORING.md](SCORING.md) describes the
authoritative output validation and cost rules.
