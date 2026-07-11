import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import { SlippagePanels } from "./SlippagePanels";

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
          long_recovery: null,
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
          long_recovery: null,
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
          long_recovery: null,
          closes: null,
          decay_buybacks: null,
        }}
      />,
    );
    expect(screen.getByText(/no stop-outs this day/i)).toBeInTheDocument();
  });
});
