# Kickoff prompt for the coding agent

Paste this to start the build:

---
Read CLAUDE.md, then spec/README.md, in this repository. The spec is a locked
contract — you build against it, you never modify it.

Begin with the build order in spec/05-architecture-ddd.md section 10:
1. First, the test harness (FakeBroker, FakeMarketData, FakeClock per doc 04's
   harness requirements) and step definitions for the generated feature files in
   tests/features/ — run `python scripts/extract_features.py` to generate them.
   Expect everything red: that is the starting condition.
2. The sandbox verification gate STP-05a (TC-STP-13) is build-blocking before any
   order-path code: verify single-leg SPXW stop support and the trigger reference
   price against the tastytrade sandbox, and report findings to me before
   proceeding.
3. Then: domain value objects and state machines -> aggregates -> event store ->
   application services against the EC-* scenarios -> adapters -> API/UI ->
   paper-mode simulator (SIM) -> full-day paper E2E.

Anything ambiguous, contradictory, or contradicted by the sandbox: stop and
propose a spec amendment to me. Never improvise around the spec.
---
