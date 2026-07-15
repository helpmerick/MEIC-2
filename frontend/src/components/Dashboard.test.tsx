import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Dashboard } from "./Dashboard";
import type { PanelState } from "../types";

const BASE: PanelState = {
  armed: true, stop_trading: false, confirm_live: true, trading_mode: "paper",
  entries_enabled: true, blocking_state: null,
};

// CAL-08 (v1.71): "the trading panel shows the active tag ... whenever the
// current ET day is tagged" — the frontend never decides this, only renders
// whatever /state's (additive) today_blackout_label says.
describe("Dashboard — CAL-08 today blackout banner", () => {
  it("shows no banner when today is untagged", () => {
    render(<Dashboard state={{ ...BASE, today_blackout_label: null }} connected />);
    expect(screen.queryByTestId("today-blackout-banner")).not.toBeInTheDocument();
  });

  it("shows 'Today: NO-TRADE — <label>' when today carries a tag", () => {
    render(<Dashboard state={{ ...BASE, today_blackout_label: "FOMC" }} connected />);
    expect(screen.getByTestId("today-blackout-banner")).toHaveTextContent("Today: NO-TRADE — FOMC");
  });

  it("omits the banner entirely when the field is absent (pre-v1.71 shape)", () => {
    render(<Dashboard state={{ ...BASE }} connected />);
    expect(screen.queryByTestId("today-blackout-banner")).not.toBeInTheDocument();
  });
});
