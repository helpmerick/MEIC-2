# MEIC Bot — Coding Agent Contract

You are building the MEIC trading bot defined ENTIRELY by the documents in `spec/`.
Read `spec/README.md` first; it is the index and contains your instructions.

## Absolute rules

1. **`spec/` is READ-ONLY.** You may never edit, rewrite, reformat, or "fix" any file
   in `spec/`, nor `spec.lock.json`, nor anything in `tests/features/` (generated from
   the spec), nor `scripts/`, nor `.github/`. These paths are hash-locked and
   owner-protected; any change fails CI and will be rejected.
2. **If the spec is ambiguous or wrong** (including anything the tastytrade sandbox
   contradicts): STOP, write a proposed spec amendment as a comment/issue/PR
   description addressed to the operator (Ash), and wait. Never improvise around
   the spec.
3. **The Gherkin in `spec/04-test-cases.md` is the definition of done.** You write
   step definitions and code until the generated features pass. You never alter a
   scenario. A test you cannot make pass is a conversation with the operator, not
   an edit.
4. **Build order** is `spec/05-architecture-ddd.md` section 10. Phase 0 is the
   sandbox verification gate STP-05a (build-blocking). Do not reorder.
5. Rule IDs (ENT-01, STP-02b, ...) must appear in code comments and test names for
   traceability. CI enforces coverage; a skipped/deleted test fails the build.
6. No trading logic in the frontend. No I/O in the domain layer. Every config value
   comes from `spec/06-configuration.md` — nothing hardcoded.
7. Work in feature branches; deliver via PR. CI (spec lock -> feature extraction ->
   traceability -> pytest) must be green.

## Commands

- `python scripts/extract_features.py`   regenerate tests/features/ from the spec
- `python scripts/verify_spec_lock.py`   verify the spec hash lock
- `python scripts/check_traceability.py` verify every rule/TC has coverage
- `pytest -q`                            full offline suite (fakes only)
- `pytest -m contract`                   sandbox contract tests (operator-triggered)
