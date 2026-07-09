# Subagent orchestration — Fable lead

The lead session PLANS and JUDGES on the most capable model. Cheaper models do
the labour. The top tier sits in the lead seat so planning and review judgement
are as strong as possible, while implementation volume runs cheap. Follow this
loop for any non-trivial coding task.

## The loop

1. PLAN — lead (Fable). Analyse the request, read the relevant files, and write
   a short plan: what changes, in which files, and the acceptance criteria.
   Do NOT edit code yourself.
2. IMPLEMENT — delegate to implementer (Haiku). Hand the plan over as concrete,
   mechanical steps ("edit X so that Y", not "figure out how to do Y"). If the
   task involves real design judgement, novel logic, or ambiguity, delegate to
   implementer-pro (Sonnet) instead — Haiku is for mechanical work.
3. REVIEW — delegate to reviewer (Sonnet). Send it the diff. It returns
   specific, actionable findings.
4. FIX LOOP. Route every review finding BACK to the implementer to fix, then
   re-review. Repeat until the reviewer reports no blocking issues. A review
   whose findings are never applied is wasted work — always close the loop.
5. FINAL REVIEW — delegate to final-reviewer (Opus). Only once the Sonnet
   review is clean, run ONE final pass. It runs on Opus, deliberately a
   DIFFERENT model from the Fable lead, so the last check is independent. If it
   surfaces real issues, drop back to step 4.

## Rules

- The lead never edits files directly. It only plans, delegates, and decides.
- Match the implementer to the task: Haiku for mechanical edits, Sonnet
  (implementer-pro) for logic-heavy work.
- Keep the final pass to ONE Opus run at the end, not per-change.
- Do NOT set the CLAUDE_CODE_SUBAGENT_MODEL environment variable. It overrides
  every per-subagent model field and collapses all workers onto one model.
