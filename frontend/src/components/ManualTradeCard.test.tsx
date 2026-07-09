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

describe("collapse / expand", () => {
  it("is collapsed by default and expands on click", async () => {
    render(<ManualTradeCard entriesEnabled />);
    expect(screen.queryByLabelText("manual target premium")).toBeNull();

    fireEvent.click(screen.getByText("Fire manual trade"));
    expect(await screen.findByLabelText("manual target premium")).toBeInTheDocument();
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
    expect(result).toHaveTextContent("$4.00");
    expect(result).toHaveTextContent(/simulation/);
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
