// RPT-11: the waterfall MUST reconcile to the cent; a residual renders an
// explicit error state, never a silently adjusted bar.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { WaterfallResult } from "../../types";
import { Waterfall } from "./Waterfall";

describe("Waterfall (RPT-11)", () => {
  it("renders the labelled bars and the final Net P&L when it reconciles", () => {
    const wf: WaterfallResult = {
      credits: "8400.00", stop_costs: "2600.00", recoveries: "310.00", buybacks: "145.00",
      fees: "220.00", slippage: "95.00", net: "5650.00", premium_capture: "0.6726190476",
    };
    render(<Waterfall wf={wf} />);
    const bar = screen.getByTestId("waterfall");
    expect(bar).toHaveTextContent(/Credits collected/);
    expect(bar).toHaveTextContent(/\+\$8400\.00/);
    expect(bar).toHaveTextContent(/Stop-out costs/);
    expect(bar).toHaveTextContent(/−\$2600\.00/);
    expect(bar).toHaveTextContent(/Net P&L/);
    expect(bar).toHaveTextContent(/\+\$5650\.00/);
    expect(bar).toHaveTextContent(/67\.3% of collected/);
  });

  it("renders an explicit error banner on a residual — never a silently adjusted bar", () => {
    const wf: WaterfallResult = {
      error: "residual", residual: "12.34", expected_net: "5650.00", computed_net: "5662.34",
    };
    render(<Waterfall wf={wf} />);
    const banner = screen.getByTestId("waterfall-error");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent(/reconciliation FAILED/i);
    expect(banner).toHaveTextContent(/residual/i);
    expect(banner).toHaveTextContent(/\$12\.34/);
    expect(banner).toHaveTextContent(/\$5650\.00/); // expected
    expect(banner).toHaveTextContent(/\$5662\.34/); // computed
    // No bars are rendered in the error state — nothing silently adjusted.
    expect(screen.queryByTestId("waterfall")).not.toBeInTheDocument();
  });
});
