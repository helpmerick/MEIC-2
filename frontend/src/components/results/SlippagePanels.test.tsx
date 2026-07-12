import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import { SlippagePanels } from "./SlippagePanels";
import type { LongRecoveryFamily } from "../../types";

const EMPTY_LONG_RECOVERY: LongRecoveryFamily = {
  rows: [], n: 0, mean: null, p50: null, p90: null, max: null,
  mean_ticks: null, nle_estimate_captured: false,
};

// Operator ruling 2026-07-11: stop-out slippage (EC-STP-03 fill − trigger,
// a PER-SHARE price figure) displays as real cash per contract (×100), so
// the 2026-07-10 C7565 stop's −0.10 gap reads "-$10", not "$-0.10". The
// ticks column stays in ticks.
describe("SlippagePanels — RPT-07 stop-outs in contract dollars", () => {
  it("renders per-share slippage figures as ×100 cash", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 1, mean: "-0.10", p50: "-0.10", p90: "-0.10", max: "-0.10", mean_ticks: "-1", },
          long_recovery: EMPTY_LONG_RECOVERY,
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    const table = screen.getByTestId("stop-out-table");
    expect(within(table).getAllByText("-$10")).toHaveLength(4); // mean/p50/p90/max
    expect(within(table).getByText("-1")).toBeInTheDocument();  // ticks stay ticks
  });

  it("a gap-through stop (positive slippage) shows as positive cash", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 2, mean: "0.25", p50: "0.20", p90: "0.30", max: "0.30", mean_ticks: "2.5" },
          long_recovery: EMPTY_LONG_RECOVERY,
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    const table = screen.getByTestId("stop-out-table");
    expect(within(table).getByText("+$25")).toBeInTheDocument();
    expect(within(table).getAllByText("+$30")).toHaveLength(2); // p90 and max
  });

  it("no stop-outs stays the honest empty state", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 0, mean: null, p50: null, p90: null, max: null, mean_ticks: null },
          long_recovery: EMPTY_LONG_RECOVERY,
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    expect(screen.getByText(/no stop-outs this day/i)).toBeInTheDocument();
  });
});

// RPT-07 long recovery (2026-07-11): now populated from journaled events —
// the family is no longer a permanent GapNote once LongSold rows exist.
describe("SlippagePanels — RPT-07 long recovery", () => {
  it("renders per-share mark/realized/buffer and contract-dollar diff/shortfall", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 0, mean: null, p50: null, p90: null, max: null, mean_ticks: null },
          long_recovery: {
            rows: [{
              entry_id: "2026-07-11#1", side: "PUT",
              mark_mid: "2.15", realized: "2.05",
              diff: "-0.10", markup: "0.10", shortfall: "-1.95",
              nle_estimate: null,
            }],
            n: 1, mean: "-0.10", p50: "-0.10", p90: "-0.10", max: "-0.10",
            mean_ticks: "-2", nle_estimate_captured: false,
          },
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    const summary = screen.getByTestId("long-recovery-summary");
    expect(within(summary).getByText("1")).toBeInTheDocument();
    expect(within(summary).getAllByText("-$10")).toHaveLength(4); // mean/p50/p90/max diff
    // UI-28 (v1.61): slippage in BOTH ticks and position dollars — the ticks
    // column stays in ticks (server-derived, EC-STP-03 tick 0.05).
    expect(within(summary).getByText("-2")).toBeInTheDocument();

    const table = screen.getByTestId("long-recovery-table");
    expect(within(table).getByText("2026-07-11#1")).toBeInTheDocument();
    expect(within(table).getByText("+2.15")).toBeInTheDocument();  // mark mid, per-share
    expect(within(table).getByText("+2.05")).toBeInTheDocument();  // realized, per-share
    expect(within(table).getByText("+0.10")).toBeInTheDocument();  // buffer, per-share
    expect(within(table).getByText("-$10")).toBeInTheDocument();   // diff, contract dollars
    expect(within(table).getByText("-$195")).toBeInTheDocument();  // shortfall, contract dollars
    expect(within(table).getByText("—", { selector: "td[title]" })).toBeInTheDocument(); // NLE gap
  });

  it("a pre-stamping row renders honest '—' for mark/buffer/diff/shortfall", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 0, mean: null, p50: null, p90: null, max: null, mean_ticks: null },
          long_recovery: {
            rows: [{
              entry_id: "2026-07-01#1", side: "CALL",
              mark_mid: null, realized: "1.80",
              diff: null, markup: null, shortfall: null,
              nle_estimate: null,
            }],
            n: 1, mean: null, p50: null, p90: null, max: null,
            mean_ticks: null, nle_estimate_captured: false,
          },
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    const table = screen.getByTestId("long-recovery-table");
    expect(within(table).getByText("+1.80")).toBeInTheDocument(); // realized always known
    // mark mid, buffer, diff, shortfall (all null -> "—"), plus the NLE column
    expect(within(table).getAllByText("—")).toHaveLength(5);
    const summary = screen.getByTestId("long-recovery-summary");
    expect(within(summary).getAllByText("—")).toHaveLength(5); // mean/p50/p90/max diff + mean ticks, all null
  });

  it("no long recoveries this day stays the honest empty state, not a GapNote", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 0, mean: null, p50: null, p90: null, max: null, mean_ticks: null },
          long_recovery: EMPTY_LONG_RECOVERY,
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    expect(screen.getByText(/no long recoveries this day/i)).toBeInTheDocument();
    expect(screen.queryByTestId("long-recovery-table")).not.toBeInTheDocument();
  });

  it("closes/decay-buybacks stay an honest GapNote (still not captured)", () => {
    render(
      <SlippagePanels
        daySlippage={{
          stop_outs: { n: 0, mean: null, p50: null, p90: null, max: null, mean_ticks: null },
          long_recovery: EMPTY_LONG_RECOVERY,
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    expect(screen.getByText(/closes \/ decay buybacks/i)).toBeInTheDocument();
    const gapNotes = screen.getAllByTestId("gap-note");
    expect(gapNotes.some((n) => /fill-vs-mark-at-initiation/i.test(n.textContent ?? ""))).toBe(true);
  });

  it("no daySlippage at all falls back to the per-day-only GapNote", () => {
    render(<SlippagePanels />);
    expect(screen.queryByTestId("long-recovery-table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("long-recovery-summary")).not.toBeInTheDocument();
  });
});
