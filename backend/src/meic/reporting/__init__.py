"""Results Dashboard (RPT-*, doc 10) — a pure, read-only reporting layer.

Principle 1 (doc 10): the reporting module has NO broker-gateway dependency
for order actions and can never place, modify, or cancel an order. This
package therefore imports nothing from `meic.adapters` or `meic.composition`
(TC-RPT-06 enforces this structurally by AST scan) — only `meic.domain`,
`meic.application`, and the stdlib. Every public function is a pure
transform: event list and/or plain Decimal/int/str values in, a value out.
Nothing is read from a global; nothing is cached as a mutable side-store of
truth (deterministic replay, doc 10 Principle 4).
"""
