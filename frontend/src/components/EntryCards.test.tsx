import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EntryCards } from "./EntryCards";
import type { EntryCard } from "../types";

function card(over: Partial<EntryCard> = {}): EntryCard {
  return {
    entry_id: "e1", status: "PROTECTED", net_credit: "4.00", pnl: "4.00",
    sides_stopped: [], sides_expired: [], recovered: false, close_initiator: null, ...over,
  };
}

describe("EntryCards — Close (UI-16)", () => {
  it("shows Close on an open entry and fires onClose instantly (no dialog)", async () => {
    const onClose = vi.fn().mockResolvedValue(undefined);
    render(<EntryCards entries={[card()]} onClose={onClose} />);

    await userEvent.click(screen.getByRole("button", { name: /^close$/i }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledWith("e1");
  });

  it("hides Close on terminal entries (CLOSED / EXPIRED / DECAY_CLOSED)", () => {
    render(
      <EntryCards
        entries={[
          card({ entry_id: "a", status: "CLOSED" }),
          card({ entry_id: "b", status: "EXPIRED" }),
          card({ entry_id: "c", status: "DECAY_CLOSED" }),
        ]}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /close/i })).toBeNull();
  });

  it("still offers Close on a stopped entry (a side may remain open)", () => {
    render(<EntryCards entries={[card({ status: "STOPPED", sides_stopped: ["PUT"] })]} onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: /^close$/i })).toBeInTheDocument();
  });
});

// FEATURE 1/2/3: placed time, per-side legs, live P/L on the card.
describe("EntryCards — placed time / legs / live P&L", () => {
  const LEGS = [
    { side: "PUT" as const, role: "short" as const, strike: "7535", price: "1.80", qty: 1 },
    { side: "PUT" as const, role: "long" as const, strike: "7510", price: "0.08", qty: 1 },
    { side: "CALL" as const, role: "short" as const, strike: "7540", price: "1.95", qty: 1 },
    { side: "CALL" as const, role: "long" as const, strike: "7565", price: "0.07", qty: 1 },
  ];
  const PREMIUM = { PUT: "1.72", CALL: "1.88" };

  it("shows the placed time as a local wall-clock HH:MM", () => {
    render(<EntryCards entries={[card({ placed_at: "2026-07-09T14:32:00+00:00" })]} onClose={vi.fn()} />);
    const expected = new Date("2026-07-09T14:32:00+00:00")
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    expect(screen.getByText(`Placed ${expected}`)).toBeInTheDocument();
  });

  it("omits the placed line when placed_at is absent", () => {
    render(<EntryCards entries={[card()]} onClose={vi.fn()} />);
    expect(screen.queryByText(/^Placed /)).toBeNull();
  });

  it("shows the per-side legs with strikes and premium received", () => {
    render(<EntryCards entries={[card({ legs: LEGS, premium_received: PREMIUM })]} onClose={vi.fn()} />);
    expect(screen.getByText("P 7535/7510 +1.72")).toBeInTheDocument();
    expect(screen.getByText("C 7540/7565 +1.88")).toBeInTheDocument();
  });

  it("shows a dash for a side's premium when a leg price is missing", () => {
    const legs = [...LEGS];
    legs[1] = { ...legs[1], price: null };  // put long price unknown
    render(<EntryCards entries={[card({ legs, premium_received: { PUT: null, CALL: "1.88" } })]} onClose={vi.fn()} />);
    expect(screen.getByText("P 7535/7510 —")).toBeInTheDocument();
  });

  it("shows live P/L in green when non-negative, with the as-of time", () => {
    render(<EntryCards entries={[card({ live_pnl: "123", live_pnl_asof: "2026-07-09T14:40:00+00:00" })]}
                       onClose={vi.fn()} />);
    const asof = new Date("2026-07-09T14:40:00+00:00").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const el = screen.getByText(`P/L $+123 (as of ${asof})`);
    expect(el).toHaveClass("pos");
  });

  it("shows live P/L in red when negative", () => {
    render(<EntryCards entries={[card({ live_pnl: "-45", live_pnl_asof: "2026-07-09T14:40:00+00:00" })]}
                       onClose={vi.fn()} />);
    const asof = new Date("2026-07-09T14:40:00+00:00").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const el = screen.getByText(`P/L $-45 (as of ${asof})`);
    expect(el).toHaveClass("neg");
  });

  it("shows a dash for live P/L when null (paper, or no fresh snapshot)", () => {
    render(<EntryCards entries={[card()]} onClose={vi.fn()} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
