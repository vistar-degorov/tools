# Tests

## Git grep fixtures

The fixtures in `git_grep_fixtures/` are generated from `corpus/gg/search/` using the cases defined in `support/git_grep_cases.py`.

Regenerate them from the repository root:

```bash
python -m test.support.generate_git_grep_fixtures
```

Verify the manifest, corpus digest, case definitions, and raw file hashes:

```bash
python -m unittest test.unit.test_git_grep_fixtures
```

The manifest records the Git version used for generation. Different Git versions may produce different output or errors.
