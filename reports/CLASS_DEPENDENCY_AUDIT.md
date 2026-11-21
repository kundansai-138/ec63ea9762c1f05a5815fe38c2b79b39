# Class-qualified schema dependencies

`TriggerProperties.arg_types` used to trigger the full-suite collection fallback
because the attribute name occurs in import-time code. The new profile records
which class owns each read and follows the import-time derivation of
`required_args`. This distinguishes unrelated expression classes that use the
same attribute name.

## Workflow

1. `build_class_map.py` validates the pinned source and traces its unchanged full
   test suite. It records qualified attribute owners, inherited dependencies,
   shared fixture contexts, direct reflection and derived required-argument sets.
2. `enrich_map.py --class-profile` verifies the profile's commit, test IDs and
   successful completion, then adds it without removing the other map profiles.
3. `class_dependencies.py` accepts only an `arg_types` assignment whose ordered,
   unique string keys stay fixed and whose values remain exact booleans.
4. `selector.py` unions the class-specific readers with constructor, source,
   reflection and uncertain-test dependencies. Existing scheduling assigns every
   selected test exactly once and accounts for the two-second worker charge.

The recognized derived assignment is specifically
`cls.required_args = {k for k, v in cls.arg_types.items() if v}`. Arbitrary import
transformations do not qualify. Same-shaped boolean replacements cannot change
the dictionary's key set or introduce type-dependent failure into that operation.

## Fallback behavior

Changed keys, ordering, value types, annotations or unsupported assignment forms
retain existing selection. Custom metaclasses, class-local schema aliases and
unknown collection consumers also retain the fallback. Escaped reflective
builtins preserve the whole test context; an escape during collection disables
narrowing. Untraced subprocess tests stay included, and background-thread reads
invalidate the profile. No-argument `vars()` and `dir()` preserve caller scope.

The target's direct readers are the DuckDB create-trigger test and two Postgres
DDL/create-trigger tests. The union with safety dependencies selects seven tests.
The profile contains 706 candidate attributes, of which 655 meet its recorded
checks. Independent full-suite schema mutation validation specifically targets
`TriggerProperties`; it does not certify arbitrary edits on every mapped class.
The `Snowflake.Tokenizer.NUMERIC_ESCAPES` fallback is unchanged.

## Validation

The map is built from the unchanged pinned baseline, without sample answers,
mutation outcomes or patch IDs. The full build passed 1,620 tests and 25,440
subtests, with one expected environment skip. Builder regression tests cover
class identity, inheritance, derived import edges, direct and aliased reflection,
class-local aliases, custom metaclasses and rejected schema shapes.

Three independent full-suite mutations (`require_referenced_table`,
`optional_table`, `reverse_all_flags`) produced 3, 0 and 3 actually failing tests.
All were retained. Choices were frozen before execution. Integration rechecked
the same choices against the recorded actual failures using the built Docker
image. The runtime and map match the independently validated candidate.

See the [submission audit](SUBMISSION_AUDIT.md) for current scores, exact scope
and limitations, and [correctness report](class-integration-correctness.json)
for preservation checks and all 34 recorded mutation rechecks.

## Rebuilding

Use the pinned preparation image and verified baseline described in the README.
After generating the existing map and other profiles, run:

```sh
python /app/build_class_map.py --source /tmp/sqlglot --source-manifest /input/manifest.json --map /output/coverage-map.json.gz --output /output/class-map.json.gz
python /app/enrich_map.py --input-map /output/coverage-map.json.gz --output-map /output/coverage-map.json.gz --class-profile /output/class-map.json.gz
```

The normal submission Dockerfile copies the committed merged map. It does not
execute the builder or mutated sqlglot tests during selection.
