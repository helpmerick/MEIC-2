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

// Operator request 2026-07-11: the headline P&L and credit line show real
// contract dollars (premium x100 x contracts), not the raw per-share Decimal.
describe("EntryCards — contract-dollar P&L and credit (operator request 2026-07-11)", () => {
  it("shows the headline P&L and credit in real dollars for a 1-contract entry", () => {
    render(<EntryCards entries={[card({ net_credit: "5.20", pnl: "0.40" })]} onClose={vi.fn()} />);
    expect(screen.getByText("+$40")).toBeInTheDocument();
    expect(screen.getByText("credit $520")).toBeInTheDocument();
  });

  it("shows a negative headline P&L in real dollars", () => {
    render(<EntryCards entries={[card({ net_credit: "5.20", pnl: "-1.05" })]} onClose={vi.fn()} />);
    expect(screen.getByText("-$105")).toBeInTheDocument();
  });

  it("scales the headline P&L and credit by the entry's own contracts count (ENT-04)", () => {
    const legs = [
      { side: "PUT" as const, role: "short" as const, strike: "7535", price: "1.80", qty: 3 },
      { side: "PUT" as const, role: "long" as const, strike: "7510", price: "0.08", qty: 3 },
    ];
    render(<EntryCards entries={[card({ net_credit: "5.20", pnl: "0.40", legs })]} onClose={vi.fn()} />);
    expect(screen.getByText("+$120")).toBeInTheDocument();
    expect(screen.getByText("credit $1560")).toBeInTheDocument();
  });
});

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

  // UC-14/TC-CLS-02 (CLS-03): "On a WORKING entry the action is Cancel entry
  // ... also instant" — the PENDING card's button must SAY so, and still fire
  // the one shared close handler (CLS-02: the UI has no close logic of its own).
  it("a PENDING (working) entry's action reads Cancel entry and fires instantly (CLS-03)", async () => {
    const onClose = vi.fn().mockResolvedValue(undefined);
    render(<EntryCards entries={[card({ status: "PENDING", net_credit: "0", pnl: "0" })]}
                       onClose={onClose} />);

    expect(screen.queryByRole("button", { name: /^close$/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /cancel entry/i }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledWith("e1");
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

  it("shows the per-side legs with strikes and premium received in real contract dollars", () => {
    // premium_received is per-share ("1.72"/"1.88"); LEGS carry qty: 1, so
    // contract dollars = premium x 100 x 1 (operator request 2026-07-11).
    render(<EntryCards entries={[card({ legs: LEGS, premium_received: PREMIUM })]} onClose={vi.fn()} />);
    expect(screen.getByText("P 7535/7510 +$172")).toBeInTheDocument();
    expect(screen.getByText("C 7540/7565 +$188")).toBeInTheDocument();
  });

  it("scales the per-side premium by the entry's own contracts count (ENT-04)", () => {
    const legs2x = LEGS.map((l) => ({ ...l, qty: 2 }));
    render(<EntryCards entries={[card({ legs: legs2x, premium_received: PREMIUM })]} onClose={vi.fn()} />);
    expect(screen.getByText("P 7535/7510 +$344")).toBeInTheDocument();
    expect(screen.getByText("C 7540/7565 +$376")).toBeInTheDocument();
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

// v1.58 TPF/TPT: profit%, floor/target set/clear, disarmed badge, TPT-06
// dollar feedback (UI-13/14/15).
describe("EntryCards — TPF/TPT exit controls", () => {
  it("shows profit% when available, a dash when not", () => {
    const { rerender } = render(
      <EntryCards entries={[card({ profit_pct: "12.5" })]} onClose={vi.fn()} />,
    );
    expect(screen.getByText("Profit: +12.5%")).toBeInTheDocument();

    rerender(<EntryCards entries={[card({ profit_pct: null })]} onClose={vi.fn()} />);
    expect(screen.getByText("Profit: —")).toBeInTheDocument();
  });

  it("sets a floor level and calls onSetFloor with the entry id and typed level", async () => {
    const onSetFloor = vi.fn().mockResolvedValue(undefined);
    render(
      <EntryCards entries={[card()]} onClose={vi.fn()} onSetFloor={onSetFloor}
                  onClearFloor={vi.fn()} onSetTarget={vi.fn()} onClearTarget={vi.fn()} />,
    );
    await userEvent.type(screen.getByLabelText("Floor level"), "20");
    await userEvent.click(screen.getAllByRole("button", { name: /^set$/i })[0]);

    expect(onSetFloor).toHaveBeenCalledWith("e1", 20);
  });

  it("shows the armed floor level and a Clear button that fires onClearFloor", async () => {
    const onClearFloor = vi.fn().mockResolvedValue(undefined);
    render(
      <EntryCards entries={[card({ tpf_floor: 20 })]} onClose={vi.fn()} onSetFloor={vi.fn()}
                  onClearFloor={onClearFloor} onSetTarget={vi.fn()} onClearTarget={vi.fn()} />,
    );
    expect(screen.getByText("Floor: 20%")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^clear$/i }));
    expect(onClearFloor).toHaveBeenCalledWith("e1");
  });

  it("shows the TPT-06 dollar feedback line while a target is armed", () => {
    render(
      <EntryCards
        entries={[card({ tpt_target: 60, tpt_feedback: { debit: "1.60", keep: "240" } })]}
        onClose={vi.fn()} onSetFloor={vi.fn()} onClearFloor={vi.fn()}
        onSetTarget={vi.fn()} onClearTarget={vi.fn()}
      />,
    );
    expect(screen.getByText("Exit armed: closes at debit ≤ $1.60 (keep ≥ $240)")).toBeInTheDocument();
  });

  it("shows a disarmed badge and hides the target's input once tpt_disarmed is true", () => {
    render(
      <EntryCards
        entries={[card({ tpt_target: 5, tpt_disarmed: true, sides_stopped: ["PUT"] })]}
        onClose={vi.fn()} onSetFloor={vi.fn()} onClearFloor={vi.fn()}
        onSetTarget={vi.fn()} onClearTarget={vi.fn()}
      />,
    );
    expect(screen.getByText("target disarmed")).toBeInTheDocument();
    expect(screen.queryByLabelText("Target level")).toBeNull();
  });

  it("omits exit controls entirely when no handlers are wired", () => {
    render(<EntryCards entries={[card({ tpf_floor: 20 })]} onClose={vi.fn()} />);
    expect(screen.queryByText("Floor: 20%")).toBeNull();
  });
});

// EOD-01 v1.59: the provisional tag while a held-to-expiry short's broker
// settlement has not yet been captured.
describe("EntryCards — settlement_pending (EOD-01 v1.59)", () => {
  it("shows a provisional tag when settlement_pending is true", () => {
    render(<EntryCards entries={[card({ settlement_pending: true })]} onClose={vi.fn()} />);
    expect(screen.getByText(/provisional — settlement pending/i)).toBeInTheDocument();
  });

  it("omits the provisional tag when settlement_pending is absent or false", () => {
    render(<EntryCards entries={[card()]} onClose={vi.fn()} />);
    expect(screen.queryByText(/provisional — settlement pending/i)).toBeNull();
  });
});
