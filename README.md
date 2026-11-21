# Blast Radius: baseline dependency selector

This submission selects sqlglot tests using dependencies measured on the
unmodified commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, then minimizes the
worker cost for that selection. It does not execute patched code at decision
time. Unsupported or uncertain changes fall back to the complete test suite.

The runtime uses only Python's standard library and the committed
`coverage-map.json.gz`. It reads `/data/tests.json` and `/data/commits/*.patch`,
writes one compact JSON answer per commit to stdout, and sends diagnostics to
stderr. It needs no network, mutable source checkout, or package installation.

## Validated sample result

All three-run and five-run Docker evaluations selected every known failing test
across the 30 samples, with identical output and **zero missed commits**.

| Measure | Result |
| --- | ---: |
| Sum of busiest-worker loads | 202.689 s |
| Worker overhead: 43 workers at 2 s | 86.000 s |
| Miss penalty: 0 commits at 300 s | 0.000 s |
| Simulated total | 288.689 s |
| Three-run median including measured CPU | 290.6 s |
| Five-run median including measured CPU | 290.5 s |
| Previous submission three-run score | 330.4 s |
| Starter baseline | 1,614.1 s |
| Peak selector RSS, sample runs | under 196 MiB |
| Output per sample batch | 24,000 bytes |
| Regression tests in pinned Docker | 97 passed |

The improvement is about 12% over the previous submission and 82% over the
starter. An independent integer-arithmetic scorer agrees with the untouched
supplied scorer. It explicitly checks the one-300-second-penalty-per-missed-commit
rule and rounds the median in integer microseconds.

The baseline map was generated in the pinned image with the exact package lock:
1,620 tests and 25,440 subtests passed, with the expected SQLite skip. Its source
and dynamic coverage are unchanged; added static indexes come only from that
baseline. An independent validation used 15 mutations outside sample-edited
lines and a fixed 18-test subset per mutation. All 53 observed failures were
selected. This subset validation does not prove safety on every hidden change.

Measured CPU includes a small measurement parent. These are local sample scores,
not official hidden-set scores. See the [submission audit](reports/SUBMISSION_AUDIT.md),
[three-run results](reports/sample-validation.json),
[five-run results](reports/sample-validation-five-runs.json),
[baseline provenance](reports/baseline-validation.json), and
[independent mutation summary](reports/heldout-summary.json).

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

- Existing function-body changes use whole-statement coverage when structure
  and lexical bindings remain unchanged. Changed conditions retain their full
  compound statement. Insertions, deletions, comprehension changes, and scope
  changes retain whole-function coverage.
- Direct multiline lambdas in dispatch dictionaries can use body coverage only
  when bytecode establishes a mandatory interior line that did not execute
  during collection. Dictionary keys, defaults, signatures, and surrounding
  expressions must stay identical; ambiguous cases retain the full suite.
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
- An AST-preserving edit still selects tests that read the source file as data.
  Comment-shaped encoding declarations are parsed rather than assumed harmless.
- Changes to fixtures and other non-Python files select their recorded readers.
  Missing reader evidence falls back to the full suite.
- Changed signatures, decorators, imports, unsupported top-level expressions,
  test code, syntax errors, unknown files, unsupported patch operations, and
  invalid or missing maps fall back to the full suite.

The builder propagates shared fixture setup to all tests in its class, module,
package, or session. Teardown dependencies are conservatively attributed to the
whole module. Baseline optimizer and executor `ProcessPoolExecutor` jobs run
inline while recording, with real multiprocessing pickle round trips for
arguments, results, initializers, and exceptions. This captures serialization
dependencies as well as child computations and fixture reads. Tests that start
other, untraced subprocesses remain conservatively included.
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
