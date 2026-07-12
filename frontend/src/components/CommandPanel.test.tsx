// The Confirm Live gate: turning it ON requires typing LIVE (real-money friction,
// ENT-01b). Turning it OFF is instant — disabling a safety gate never has friction.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { CommandPanel } from "./CommandPanel";
import type { PanelState } from "../types";

function state(over: Partial<PanelState> = {}): PanelState {
  return {
    armed: false, stop_trading: false, confirm_live: false, trading_mode: "paper",
    entries_enabled: false, blocking_state: "CONFIRM_LIVE_OFF", ...over,
  };
}

const noop = () => {};

beforeEach(() => vi.restoreAllMocks());

describe("Confirm Live — type-LIVE gate", () => {
  it("opens a modal instead of flipping on immediately", async () => {
    const spy = vi.spyOn(api, "confirmLive");
    render(<CommandPanel state={state()} optimistic={noop} refresh={noop} />);

    fireEvent.click(screen.getByText("Confirm Live: OFF"));

    await screen.findByRole("dialog");
    expect(spy).not.toHaveBeenCalled();                 // clicking is not confirming
  });

  it("keeps the button disabled until the exact word LIVE is typed", async () => {
    render(<CommandPanel state={state()} optimistic={noop} refresh={noop} />);
    fireEvent.click(screen.getByText("Confirm Live: OFF"));
    await screen.findByRole("dialog");

    const confirm = screen.getByRole("button", { name: /^confirm live$/i });
    const input = screen.getByLabelText("type LIVE to confirm");

    expect(confirm).toBeDisabled();
    fireEvent.change(input, { target: { value: "live please" } });
    expect(confirm).toBeDisabled();                     // must be EXACTLY live
    fireEvent.change(input, { target: { value: "LIVE" } });
    expect(confirm).toBeEnabled();
  });

  it("confirms on typing LIVE and pressing the button", async () => {
    const spy = vi.spyOn(api, "confirmLive").mockResolvedValue(state({ confirm_live: true }));
    render(<CommandPanel state={state()} optimistic={noop} refresh={noop} />);
    fireEvent.click(screen.getByText("Confirm Live: OFF"));
    await screen.findByRole("dialog");

    fireEvent.change(screen.getByLabelText("type LIVE to confirm"), { target: { value: "LIVE" } });
    fireEvent.click(screen.getByRole("button", { name: /^confirm live$/i }));

    await waitFor(() => expect(spy).toHaveBeenCalledWith(true));
  });

  it("Enter submits when LIVE is typed (the key that did nothing before)", async () => {
    const spy = vi.spyOn(api, "confirmLive").mockResolvedValue(state({ confirm_live: true }));
    render(<CommandPanel state={state()} optimistic={noop} refresh={noop} />);
    fireEvent.click(screen.getByText("Confirm Live: OFF"));
    const input = await screen.findByLabelText("type LIVE to confirm");

    fireEvent.change(input, { target: { value: "xxx" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(spy).not.toHaveBeenCalled();                 // Enter does nothing until LIVE

    fireEvent.change(input, { target: { value: "LIVE" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(spy).toHaveBeenCalledWith(true));
  });

  it("names the session honestly: LIVE = real money, paper = simulator", async () => {
    const { unmount } = render(<CommandPanel state={state({ trading_mode: "live" })} optimistic={noop} refresh={noop} />);
    fireEvent.click(screen.getByText("Confirm Live: OFF"));
    await screen.findByRole("dialog");
    expect(screen.getByRole("alert")).toHaveTextContent(/real money/i);
    unmount();

    render(<CommandPanel state={state({ trading_mode: "paper" })} optimistic={noop} refresh={noop} />);
    fireEvent.click(screen.getByText("Confirm Live: OFF"));
    await screen.findByRole("dialog");
    expect(screen.getByText(/simulator/i)).toBeInTheDocument();
    expect(screen.getByText(/real money requires launching the live app/i)).toBeInTheDocument();
  });

  it("turning Confirm Live OFF is instant — no modal", async () => {
    const spy = vi.spyOn(api, "confirmLive").mockResolvedValue(state({ confirm_live: false }));
    render(<CommandPanel state={state({ confirm_live: true })} optimistic={noop} refresh={noop} />);

    fireEvent.click(screen.getByText("Confirm Live: ON"));

    expect(screen.queryByRole("dialog")).toBeNull();     // disabling a gate has no friction
    await waitFor(() => expect(spy).toHaveBeenCalledWith(false));
  });
});
