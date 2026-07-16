import { afterEach, describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// The live hook opens a WebSocket — mock it so App renders controlled data.
vi.mock("./useLiveBot", () => ({
  useLiveBot: () => ({
    state: {
      armed: true, stop_trading: false, confirm_live: true, trading_mode: "paper",
      entries_enabled: true, blocking_state: null,
    },
    report: null,
    entries: [{
      entry_id: "e1", status: "PROTECTED", net_credit: "4.00", pnl: "4.00",
      sides_stopped: [], sides_expired: [], recovered: false, close_initiator: null,
    }],
    activity: [],
    connected: true, error: null, optimistic: vi.fn(), refresh: vi.fn(),
  }),
}));

import { App } from "./App";
import { api, ApiError } from "./api";

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }));
  // App now mounts the SchedulePanel, which loads the composed schedule on mount.
  vi.spyOn(api, "getSchedule").mockResolvedValue({
    rows: [], day_total_estimate: "0", max_day_risk: null, headroom: null,
    exceeds_max_day_risk: false, config_version: null, estimate_note: "", risk_scope_note: "",
  });
});

describe("App — Close / Flatten (UI-16 / TC-FLT-01)", () => {
  it("Close on a card calls api.closeEntry and shows a toast (no blocking dialog)", async () => {
    const spy = vi.spyOn(api, "closeEntry").mockResolvedValue({ result: "closed" });
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: /^close$/i }));

    expect(spy).toHaveBeenCalledWith("e1");
    await waitFor(() => expect(screen.getByText(/closed e1/i)).toBeInTheDocument());
  });

  it("a cancelled working entry toasts as a cancel, not a close (CLS-03)", async () => {
    const spy = vi.spyOn(api, "closeEntry").mockResolvedValue({ result: "cancelled" });
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: /^close$/i }));

    expect(spy).toHaveBeenCalledWith("e1");
    await waitFor(() => expect(screen.getByText(/cancelled entry e1/i)).toBeInTheDocument());
  });

  it("race_detected surfaces as an error toast, never a clean cancel (CLS-03 race guard)", async () => {
    vi.spyOn(api, "closeEntry").mockResolvedValue({ result: "race_detected" });
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: /^close$/i }));

    const toast = await waitFor(() => screen.getByText(/cancel raced a fill/i));
    expect(toast.closest(".toast")?.className).toContain("err");
  });

  it("Flatten all does nothing when the operator cancels the confirmation", async () => {
    const spy = vi.spyOn(api, "flatten").mockResolvedValue({ result: "flattened", entries: [] });
    vi.spyOn(window, "prompt").mockReturnValue(null); // cancelled

    render(<App />);
    await userEvent.click(screen.getByRole("button", { name: /flatten all/i }));

    expect(spy).not.toHaveBeenCalled();
  });

  it("Flatten all with the typed FLATTEN calls api.flatten and toasts the count", async () => {
    const spy = vi.spyOn(api, "flatten").mockResolvedValue({ result: "flattened", entries: ["e1", "e2"] });
    vi.spyOn(window, "prompt").mockReturnValue("FLATTEN");

    render(<App />);
    await userEvent.click(screen.getByRole("button", { name: /flatten all/i }));

    expect(spy).toHaveBeenCalledWith("FLATTEN");
    await waitFor(() => expect(screen.getByText(/flattened 2 entries/i)).toBeInTheDocument());
  });

  // 2026-07-16 operator order (an INFORMED reversal of v1.76 — the operator
  // saw the "Removal was rejected" text and repeated the order): the drill's
  // visible trigger — the "Operational tools" disclosure AND the Outage-drill
  // button — is gone from the UI entirely. Only the trigger: the WIRING stays
  // untouched (runOutageDrill's typed-DRILL gate, api.outageDrill, the
  // /drill/outage endpoint and every backend UC-12 piece, all pinned by their
  // own backend tests, which this change does not touch).
  it("the UC-12 drill surface remains wired (api.outageDrill) even though its UI trigger is removed", () => {
    // The frontend's half of the retained wiring: the API call the (removed)
    // button used still exists and is callable — reinstatement needs only a
    // trigger, never a rebuild. The backend /drill/outage endpoint's own
    // tests (tests/application/test_drills.py, the drill-guidance test in
    // tests/application/test_live_app.py) pin the other half.
    expect(typeof api.outageDrill).toBe("function");
  });

  it("shows mode as a status tag reflecting the running process, not a switch", async () => {
    // paper_app reports PAPER; the tag is not a button (you switch by launching
    // the other process, not by clicking here).
    render(<App />);
    const tag = await screen.findByTitle(/Launch live_app/);   // the header mode tag
    expect(tag).toHaveTextContent(/PAPER/);
    expect(tag.tagName).toBe("SPAN");                          // status, not <button>
  });

  it("saves the User Password and confirms it was accepted by the backend", async () => {
    localStorage.removeItem("meic_api_token");
    const check = vi.spyOn(api, "authCheck").mockResolvedValue({ ok: true });

    render(<App />);
    await userEvent.click(screen.getByLabelText("user password"));   // 🔓
    const input = await screen.findByLabelText("user password");
    await userEvent.type(input, "s3cr3t-token");
    await userEvent.click(screen.getByRole("button", { name: /save user password/i }));

    expect(localStorage.getItem("meic_api_token")).toBe("s3cr3t-token");
    expect(check).toHaveBeenCalled();                        // validated, not blindly stored
    // control flips to "Unlocked" with a green tick, and a popup flashes the result
    await waitFor(() =>
      expect(screen.getByLabelText("user password")).toHaveTextContent(/unlocked/i));
    expect(screen.getByLabelText("password correct")).toHaveTextContent("✓");
    expect(screen.getByText(/password accepted/i)).toBeInTheDocument();  // the popup
  });

  it("shows a red cross and a popup when the User Password is wrong (401)", async () => {
    localStorage.removeItem("meic_api_token");
    vi.spyOn(api, "authCheck").mockRejectedValue(new ApiError(401, "missing_or_bad_token"));

    render(<App />);
    await userEvent.click(screen.getByLabelText("user password"));
    await userEvent.type(await screen.findByLabelText("user password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /save user password/i }));

    await waitFor(() => expect(screen.getByLabelText("password wrong")).toHaveTextContent("✗"));
    expect(screen.getByText(/wrong password/i)).toBeInTheDocument();     // the popup
  });
});

// UI-27: the Results dashboard is a separate client-side route inside this
// one SPA, sharing the shell. These pin the hash-router wiring itself; the
// Results page's own content is covered by results/ResultsPage.test.tsx.
describe("App — Results routing (UI-27)", () => {
  beforeEach(() => {
    window.location.hash = "";
    vi.spyOn(api, "getReportSummary").mockResolvedValue({
      mode: "paper",
      period_days: [],
      trust: { status: "bot-computed", confirmed_days: 0, total_days: 0, label: "0/0 days broker-confirmed" },
      core: {
        net_pnl: "0.00", gross_pnl: "0.00", fees: "0.00", filled: 0, fired: 0,
        skipped_by_reason: {}, total_credit: "0.00", day_win_rate: null,
        entry_win_rate: null, premium_capture: null,
      },
      metrics: { status: "unconfigured" },
      taxonomy: { distribution: {}, contract_breaches: [] },
      health: {
        skip_reason_histogram: {}, watchdog_escalations: 0, unprotected_events: 0,
        rsk03_mismatches: 0, correction_count: 0, ent10_crash_alerts: null, ord08_terminal_retries: null,
      },
      waterfall: {
        credits: "0.00", stop_costs: "0.00", recoveries: "0.00", buybacks: "0.00",
        fees: "0.00", slippage: "0.00", net: "0.00", premium_capture: null,
      },
    });
    vi.spyOn(api, "getDailySeries").mockResolvedValue([]);
    vi.spyOn(api, "getDayStatus").mockResolvedValue({ started: false, running: false, armed: false });
    vi.spyOn(api, "getReportDay").mockResolvedValue({
      date: "2026-07-10", mode: "paper",
      trust: { status: "bot-computed", confirmed_days: 0, total_days: 1, label: "0/1 days broker-confirmed" },
      entries: [], skips: [],
      timeline: { marks: [], markers: [] },
      slippage: {
        stop_outs: { mean: null, p50: null, p90: null, max: null, mean_ticks: null, n: 0 },
        long_recovery: { rows: [], n: 0, mean: null, p50: null, p90: null, max: null, nle_estimate_captured: false },
        closes: null, decay_buybacks: null,
      },
      corrections: [],
    });
  });

  afterEach(() => { window.location.hash = ""; });

  it("default hash renders the Trading page", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: /control/i })).toBeInTheDocument(); // combined Control card
    expect(screen.queryByTestId("results-page")).not.toBeInTheDocument();
  });

  it("clicking Results switches instantly to the Results dashboard", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("link", { name: "Results" }));
    expect(await screen.findByTestId("results-page")).toBeInTheDocument();
  });

  it("a #/results/day/... deep link renders the day drill-down directly", async () => {
    window.location.hash = "#/results/day/2026-07-10";
    render(<App />);
    expect(await screen.findByTestId("day-drilldown")).toBeInTheDocument();
    expect(screen.getByText(/Day drill-down — 2026-07-10/i)).toBeInTheDocument();
  });

  it("a day drill-down with imported fills renders the RPT-16 table and badge", async () => {
    vi.spyOn(api, "getReportDay").mockResolvedValue({
      date: "2026-07-09", mode: "live",
      trust: { status: "broker-imported", confirmed_days: 0, total_days: 1, label: "broker-imported", imported_days: 1 },
      entries: [], skips: [],
      timeline: { marks: [], markers: [] },
      slippage: {
        stop_outs: { mean: null, p50: null, p90: null, max: null, mean_ticks: null, n: 0 },
        long_recovery: { rows: [], n: 0, mean: null, p50: null, p90: null, max: null, nle_estimate_captured: false },
        closes: null, decay_buybacks: null,
      },
      corrections: [],
      imported_fills: [{
        order_id: "482214732", symbol: "SPXW  260709P05600000", action: "Sell to Open",
        quantity: 1, price: "3.00", fee: "1.42", at: "2026-07-09T14:31:00-04:00",
      }, {
        // RPT-16 settlement import (operator ruling 2026-07-10): a broker
        // Receive-Deliver row -- action is the sub_type, `value` the signed
        // net cash effect in real dollars.
        order_id: "482390058", symbol: "SPXW  260709C07540000", action: "Cash Settled Assignment",
        quantity: 1, price: "7540.00", fee: "5.00", at: "2026-07-09T22:00:00-04:00",
        value: "-369.00",
      }],
      imported_cash: { net: "-13.88", fees: "9.88" },
    });
    window.location.hash = "#/results/day/2026-07-09";
    render(<App />);
    await screen.findByTestId("day-drilldown");
    expect(screen.getByTestId("trust-badge")).toHaveTextContent("broker-imported");
    const table = screen.getByTestId("imported-fills-table");
    expect(table).toHaveTextContent("482214732");
    expect(table).toHaveTextContent("Sell to Open");
    // Settlement row renders its sub_type action and signed value, styled distinctly.
    expect(table).toHaveTextContent("Cash Settled Assignment");
    expect(table).toHaveTextContent("-369.00");
    const settlementCell = screen.getByText("Cash Settled Assignment");
    expect(settlementCell.closest("tr")).toHaveClass("imported-settlement-row");
  });

  it("the Trading page keeps its Flatten all button; Results does not", async () => {
    render(<App />);
    expect(screen.getByRole("button", { name: /flatten all/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Results" }));
    await screen.findByTestId("results-page");
    expect(screen.queryByRole("button", { name: /flatten all/i })).not.toBeInTheDocument();
  });
});

// 2026-07-16 operator order — an INFORMED reversal of v1.76 (the operator saw
// the ratified "Removal was rejected ... never removed" text and repeated:
// "remove it completely, the wiring can stay but the button must go"). These
// pin the NEW truth: no "Operational tools" disclosure and no Outage-drill
// button anywhere in the DOM — while the drill WIRING survives untouched
// (runOutageDrill + typed-DRILL gate in App.tsx, api.outageDrill, the
// /drill/outage endpoint and all backend UC-12 code + NFR-07 registry
// entries, each pinned by its own backend tests). Until the adviser's spec
// delta lands, DOC-06 step 9 / guide ch.7 describe a control that no longer
// exists — known, flagged spec-text staleness handled by amendment.
describe("App — the drill trigger is removed from the UI (operator order 2026-07-16)", () => {
  it("renders no Operational tools disclosure on the Trading page", () => {
    render(<App />);
    expect(screen.queryByRole("button", { name: /operational tools/i })).not.toBeInTheDocument();
  });

  it("renders no Outage drill button anywhere on the Trading page", () => {
    render(<App />);
    expect(screen.queryByRole("button", { name: /outage drill/i })).not.toBeInTheDocument();
    // and no stray text remnant of the removed trigger either
    expect(screen.queryByText(/operational tools/i)).not.toBeInTheDocument();
  });
});

// CAL-08/UI-30 + DOC-01/DOC-05/DOC-06/UI-32 (v1.75 commission): the nav is
// fixed at exactly five tabs, in the ruled order — Trading | Results |
// Calendar | How it works | Getting started.
describe("App — nav (v1.75 five-tab commission)", () => {
  beforeEach(() => {
    window.location.hash = "";
    vi.spyOn(api, "getCalendar").mockResolvedValue({ available: true, tags: {}, staleness: {}, standing_rules: {} });
    // DOC-05 (doc 12, slice 4): the guide's own read model. Harmless when the
    // route never reaches How it works (most tests in this block); the
    // "clicking How it works" test below overrides this with fuller content.
    vi.spyOn(api, "getGuide").mockResolvedValue({
      guide_markdown: "# THE GUIDE (ratified content, v1.72 — describes spec v1.72; DOC-05 stamp)\n\n"
        + "## 1. What the bot trades\n\nbody\n",
      guide_version: "1.72", running_spec_version: "1.72", version_mismatch: false,
      version_unknown: false,
    });
    // DOC-06/UI-32 (doc 12, slice 6): the fifth tab's own read model. Same
    // harmless-when-unvisited note as getGuide above; the "clicking Getting
    // started" test below actually renders it.
    vi.spyOn(api, "getGettingStarted").mockResolvedValue({
      getting_started_markdown:
        "# GETTING STARTED (ratified content, v1.79 — describes spec v1.79 and the "
        + "build's true run procedure; DOC-05 stamp)\n\n"
        + "## 1. Prerequisites, and how this build actually runs\n\nbody\n",
      getting_started_version: "1.79", running_spec_version: "1.79",
      version_mismatch: false, version_unknown: false,
    });
  });
  afterEach(() => { window.location.hash = ""; });

  it("shows exactly the five nav tabs, in order", () => {
    render(<App />);
    const nav = screen.getByRole("navigation", { name: /pages/i });
    const links = within(nav).getAllByRole("link");
    expect(links.map((l) => l.textContent))
      .toEqual(["Trading", "Results", "Calendar", "How it works", "Getting started"]);
  });

  it("clicking Calendar switches instantly to the Calendar tab (CAL-08/UI-30)", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("link", { name: "Calendar" }));
    expect(await screen.findByTestId("calendar-page")).toBeInTheDocument();
  });

  it("clicking How it works renders the ratified guide, not a placeholder (DOC-01/DOC-05)", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("link", { name: "How it works" }));
    expect(await screen.findByTestId("how-it-works-page")).toBeInTheDocument();
    expect(await screen.findByTestId("guide-version-stamp")).toHaveTextContent("v1.72");
    expect(screen.queryByTestId("how-it-works-placeholder")).not.toBeInTheDocument();
  });

  it("clicking Getting started renders the ratified fifth tab with its OWN stamp (DOC-06/UI-32)", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("link", { name: "Getting started" }));
    expect(await screen.findByTestId("getting-started-page")).toBeInTheDocument();
    // v1.79 — this mock's own section stamp, not the guide mock's v1.72 one
    // (each tab banners against its own stamp; the two must never bleed).
    expect(await screen.findByTestId("getting-started-version-stamp")).toHaveTextContent("v1.79");
  });

  it("the Calendar tab is not the Trading page — Outage drill/Flatten are hidden there too", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("link", { name: "Calendar" }));
    await screen.findByTestId("calendar-page");
    expect(screen.queryByRole("button", { name: /outage drill/i })).not.toBeInTheDocument();
  });
});
