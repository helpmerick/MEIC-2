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
