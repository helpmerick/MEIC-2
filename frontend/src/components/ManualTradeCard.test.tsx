// ENT-11/UI-25: the ad-hoc manual-trade card holds NO trading logic (UI-03) —
// these tests pin what it renders and what it sends, never that it decides.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api";
import { ManualTradeCard } from "./ManualTradeCard";
import type { ManualSimulation } from "../types";

const SIM_OK: ManualSimulation = {
  result: "ok",
  put_short: "7535", put_long: "7510",
  call_short: "7540", call_long: "7565",
  put_mid: "3.10", call_mid: "2.90",
  net_credit: "4.00",
  worst_case: "4600",
  contracts: 1,
  estimate_note: "simulation — the real fire re-selects from fresh data and may differ",
};

beforeEach(() => {
  vi.restoreAllMocks();
  // crypto.randomUUID is used to mint press_id — stub it deterministically.
  vi.stubGlobal("crypto", { randomUUID: () => "uuid-1" });
});

describe("always visible", () => {
  it("shows the trade fields immediately — no dropdown (operator request 2026-07-12)", () => {
    render(<ManualTradeCard entriesEnabled />);
    expect(screen.getByText("Fire manual trade")).toBeInTheDocument();
    expect(screen.getByLabelText("manual target premium")).toBeInTheDocument();
  });
});

describe("Simulate (read-only, UI-25)", () => {
  it("renders strikes, mids, and credit from a mocked api.manualSimulate", async () => {
    const sim = vi.spyOn(api, "manualSimulate").mockResolvedValue(SIM_OK);
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.click(screen.getByText("Simulate trade"));
    await waitFor(() => expect(sim).toHaveBeenCalled());

    const result = await screen.findByTestId("manual-sim-result");
    expect(result).toHaveTextContent("P 7535/7510");
    expect(result).toHaveTextContent("C 7540/7565");
    // net_credit is per-share ("4.00"); contracts: 1 -> $400 real cash
    // (operator request 2026-07-11). worst_case ("4600") is already dollars.
    expect(result).toHaveTextContent("$400");
    expect(result).toHaveTextContent("$4600");
    expect(result).toHaveTextContent(/simulation/);
  });

  it("scales net credit by the simulated contracts count (ENT-04)", async () => {
    vi.spyOn(api, "manualSimulate").mockResolvedValue({ ...SIM_OK, net_credit: "4.00", contracts: 3 });
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.click(screen.getByText("Simulate trade"));
    const result = await screen.findByTestId("manual-sim-result");
    // 4.00 * 100 * 3 contracts = $1200
    expect(result).toHaveTextContent("$1200");
  });

  it("renders the skip reason when selection fails", async () => {
    vi.spyOn(api, "manualSimulate").mockResolvedValue({ result: "skipped", reason: "incomplete_chain" });
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.click(screen.getByText("Simulate trade"));
    expect(await screen.findByText(/incomplete_chain/)).toBeInTheDocument();
  });

  it("works regardless of armed state — Simulate is never gated on entriesEnabled", async () => {
    const sim = vi.spyOn(api, "manualSimulate").mockResolvedValue(SIM_OK);
    render(<ManualTradeCard entriesEnabled={false} />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.click(screen.getByText("Simulate trade"));
    await waitFor(() => expect(sim).toHaveBeenCalled());
    expect(await screen.findByTestId("manual-sim-result")).toBeInTheDocument();
  });
});

describe("Fire (ENT-11)", () => {
  it("is disabled when entriesEnabled is false", () => {
    render(<ManualTradeCard entriesEnabled={false} />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    expect(screen.getByText("Fire")).toBeDisabled();
  });

  it("opens a confirm dialog and calls api.manualFire with confirmed:true and a press_id", async () => {
    const fire = vi.spyOn(api, "manualFire").mockResolvedValue({ result: "filled", initiator: "manual_entry" });
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.change(screen.getByLabelText("manual contracts"), { target: { value: "3" } });
    fireEvent.click(screen.getByText("Fire"));
    await screen.findByRole("dialog");

    fireEvent.click(screen.getByText("OK"));
    await waitFor(() => expect(fire).toHaveBeenCalled());

    const call = fire.mock.calls[0][0];
    expect(call.confirmed).toBe(true);
    expect(call.press_id).toBe("uuid-1");
    expect(call.contracts).toBe(3);

    expect(await screen.findByText(/filled \(manual_entry\)/)).toBeInTheDocument();
  });

  it("cancel closes the dialog without firing", async () => {
    const fire = vi.spyOn(api, "manualFire");
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.click(screen.getByText("Fire"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("Cancel"));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(fire).not.toHaveBeenCalled();
  });
});

// ENT-09b v1.57 (finished to spec): the floor pickers are DROPDOWNS populated
// from the live validated universe via api.manualFloorCandidates — free
// numeric entry is gone (it could name a strike that doesn't exist).
describe("floor dropdowns (ENT-09b v1.57)", () => {
  const CANDIDATES = {
    available: true,
    put: [
      { strike: "7535", distance_pts: "15", mid: "3.10" },
      { strike: "7530", distance_pts: "20", mid: "2.80" },
    ],
    call: [
      { strike: "7565", distance_pts: "15", mid: "2.90" },
      { strike: "7570", distance_pts: "20", mid: "2.50" },
    ],
    spot: "7550",
    quote_at: "2026-07-11T15:00:00+00:00",
  };

  it("fetches and renders live candidates as dropdown options when the toggle is enabled", async () => {
    const fc = vi.spyOn(api, "manualFloorCandidates").mockResolvedValue(CANDIDATES);
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));

    fireEvent.click(screen.getByLabelText("enable minimum strike floors"));
    await waitFor(() => expect(fc).toHaveBeenCalled());

    const putSelect = await screen.findByLabelText("manual put floor");
    const callSelect = screen.getByLabelText("manual call floor");
    expect(putSelect.tagName).toBe("SELECT");
    expect(putSelect).not.toBeDisabled();
    expect(callSelect).not.toBeDisabled();
    expect(putSelect).toHaveTextContent("7535 (15 pts) · $3.10");
    expect(putSelect).toHaveTextContent("7530 (20 pts) · $2.80");
    expect(callSelect).toHaveTextContent("7565 (15 pts) · $2.90");
  });

  it("no free-text numeric entry remains for the floors", async () => {
    vi.spyOn(api, "manualFloorCandidates").mockResolvedValue(CANDIDATES);
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    fireEvent.click(screen.getByLabelText("enable minimum strike floors"));

    await screen.findByLabelText("manual put floor");
    expect(screen.getByLabelText("manual put floor").tagName).toBe("SELECT");
    expect(screen.getByLabelText("manual call floor").tagName).toBe("SELECT");
  });

  it("selecting a strike carries it through to the fire request", async () => {
    vi.spyOn(api, "manualFloorCandidates").mockResolvedValue(CANDIDATES);
    const fire = vi.spyOn(api, "manualFire").mockResolvedValue({ result: "filled", initiator: "manual_entry" });
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    fireEvent.click(screen.getByLabelText("enable minimum strike floors"));

    await screen.findByLabelText("manual put floor");
    fireEvent.change(screen.getByLabelText("manual put floor"), { target: { value: "7535" } });

    fireEvent.click(screen.getByText("Fire"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("OK"));
    await waitFor(() => expect(fire).toHaveBeenCalled());

    expect(fire.mock.calls[0][0].put_floor).toBe("7535");
  });

  it("disables the dropdown with an honest note when there are no candidates (stale snapshot)", async () => {
    vi.spyOn(api, "manualFloorCandidates").mockResolvedValue({
      available: true, put: [], call: [], spot: null, quote_at: null,
    });
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    fireEvent.click(screen.getByLabelText("enable minimum strike floors"));

    await waitFor(() => expect(screen.getByLabelText("manual put floor")).toBeDisabled());
    expect(screen.getByLabelText("manual call floor")).toBeDisabled();
    expect(screen.getByText(/no live candidates yet/)).toBeInTheDocument();
  });

  it("disables the dropdown with an honest note when no candidate provider is wired", async () => {
    vi.spyOn(api, "manualFloorCandidates").mockResolvedValue({ available: false });
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    fireEvent.click(screen.getByLabelText("enable minimum strike floors"));

    await waitFor(() => expect(screen.getByLabelText("manual put floor")).toBeDisabled());
    expect(screen.getByText(/no live chain wired/)).toBeInTheDocument();
  });

  it("disables the dropdown with an honest note when the request itself fails", async () => {
    vi.spyOn(api, "manualFloorCandidates").mockRejectedValue(new ApiError(500, "boom"));
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    fireEvent.click(screen.getByLabelText("enable minimum strike floors"));

    await waitFor(() => expect(screen.getByLabelText("manual put floor")).toBeDisabled());
    expect(screen.getByText(/candidates unavailable/)).toBeInTheDocument();
  });
});

describe("validation errors surfaced from the backend (UI-03)", () => {
  it("shows a 422 error from Simulate without deciding anything itself", async () => {
    vi.spyOn(api, "manualSimulate").mockRejectedValue(
      new ApiError(422, { errors: [{ field: "contracts", reason: "out_of_range", index: 0 }] }),
    );
    render(<ManualTradeCard entriesEnabled />);
    fireEvent.click(screen.getByText("Fire manual trade"));
    fireEvent.click(screen.getByText("Simulate trade"));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
