# MEIC Trading Bot

Automated Multiple-Entry Iron Condor executor for SPX 0DTE via the tastytrade API.

**The contract lives in [`spec/`](spec/README.md).** Everything in `spec/` is the
operator-ratified specification (v1.36): rules, edge cases, Gherkin acceptance
tests, DDD architecture, configuration, and flow diagrams. It is hash-locked
(`spec.lock.json`) and owner-protected (`CODEOWNERS`) — coding agents build
against it and may never modify it.

Coding agents: read [`CLAUDE.md`](CLAUDE.md) first, then `spec/README.md`.

CI: spec lock → feature extraction → traceability → pytest. Green CI against
unmodified spec is the only definition of done.
