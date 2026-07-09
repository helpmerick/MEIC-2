import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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

  it("Outage drill runs and shows the evidence banner with the honesty caveat", async () => {
    const spy = vi.spyOn(api, "outageDrill").mockResolvedValue({
      outage_seconds: 2,
      stops_before: [{ order_id: "s1", received_at: "t", entry_id: "e1", leg: "short_put" }],
      stops_after: [{ order_id: "s1", received_at: "t", entry_id: "e1", leg: "short_put" }],
      survived: true, timestamps_unbroken: true,
      honesty_note: "PAPER: … not broker-side independence (SIM-06). … TC-STP-08 …",
    });

    render(<App />);
    await userEvent.click(screen.getByRole("button", { name: /outage drill/i }));

    expect(spy).toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/stop independence drill passed/i)).toBeInTheDocument());
    expect(screen.getByText(/timestamps unbroken/i)).toBeInTheDocument();
    expect(screen.getByText(/SIM-06/)).toBeInTheDocument(); // honesty caveat shown
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
    // padlock flips to 🔐 (accepted) — no page reload
    await waitFor(() =>
      expect(screen.getByLabelText("user password")).toHaveTextContent("🔐"));
  });

  it("tells the operator when the User Password is wrong (401), staying unlocked", async () => {
    localStorage.removeItem("meic_api_token");
    vi.spyOn(api, "authCheck").mockRejectedValue(new ApiError(401, "missing_or_bad_token"));

    render(<App />);
    await userEvent.click(screen.getByLabelText("user password"));
    await userEvent.type(await screen.findByLabelText("user password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /save user password/i }));

    await waitFor(() => expect(screen.getByText(/wrong password/i)).toBeInTheDocument());
  });
});
