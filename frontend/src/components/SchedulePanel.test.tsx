// The schedule panel holds NO trading logic (UI-03). These tests pin what it
// RENDERS and what it SENDS — never that it decides anything.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api";
import { SchedulePanel } from "./SchedulePanel";
import type { FirePreview, Preflight, ScheduleView } from "../types";

const VIEW: ScheduleView = {
  rows: [
    { time: "10:00", contracts: 2, target_premium: "3.00", wing_width: "50", stop_loss_pct: 95, worst_case_estimate: "9400.00" },
    { time: "11:15", contracts: 1, target_premium: "3.00", wing_width: "50", stop_loss_pct: 95, worst_case_estimate: "4700.00" },
  ],
  day_total_estimate: "14100.00",
  max_day_risk: "20000",
  headroom: "5900.00",
  exceeds_max_day_risk: false,
  config_version: "v3",
  estimate_note: "worst case ESTIMATED from row parameters; RSK-04 re-prices from real strikes at fire time",
  risk_scope_note: "max day risk caps BOT-PLACED risk only",
};

const PREVIEW: FirePreview = {
  press_id: "press-abc",
  entry_number: 1,
  now: "2026-07-06T10:07:00Z",
  contracts: 2,
  target_premium: "3.00",
  wing_width: "50",
  stop_loss_pct: 95,
  worst_case_estimate: "9400.00",
  worst_case_is_estimate: true,
  estimate_formula: "(width - target premium) x 100 x contracts",
  can_fire: true,
};

const PREFLIGHT: Preflight = {
  passed: false,
  blocked_by: "reconcile",
  checks: [
    { name: "schedule", rule: "ENT-01a", passed: true, detail: "2 entries composed" },
    { name: "reconcile", rule: "REC-02", passed: false, detail: "mismatch open" },
  ],
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "getSchedule").mockResolvedValue(VIEW);
});

async function renderPanel(entriesEnabled = true) {
  render(<SchedulePanel entriesEnabled={entriesEnabled} />);
  await screen.findByRole("table");
}

describe("composing the schedule", () => {
  it("renders each row's own contracts and its worst-case estimate (ENT-04)", async () => {
    await renderPanel();
    expect(screen.getByLabelText("contracts 1")).toHaveValue(2);
    expect(screen.getByLabelText("contracts 2")).toHaveValue(1);
    expect(screen.getByTestId("wc-0")).toHaveTextContent("$9400.00");
    expect(screen.getByTestId("wc-1")).toHaveTextContent("$4700.00");
  });

  it("shows max_day_risk beside the day total, so adding a row visibly eats headroom", async () => {
    await renderPanel();
    expect(screen.getByTestId("risk-readout")).toHaveTextContent("$14100.00");
    expect(screen.getByTestId("headroom")).toHaveTextContent("$5900.00");
    expect(screen.getByLabelText("max day risk")).toHaveValue("20000");
  });

  it("labels the day total an estimate and names RSK-04 as authoritative", async () => {
    await renderPanel();
    expect(screen.getByText(/ESTIMATED/)).toBeInTheDocument();
    expect(screen.getByText(/RSK-04 re-prices from real strikes/)).toBeInTheDocument();
  });

  it("discloses that max_day_risk covers bot-placed risk only (RSK-04 v1.49)", async () => {
    vi.spyOn(api, "getSchedule").mockResolvedValue({
      ...VIEW, risk_scope_note: "max day risk caps BOT-PLACED risk only � foreign positions excluded",
    });
    await renderPanel();
    expect(screen.getByTestId("risk-scope")).toHaveTextContent(/BOT-PLACED risk only/);
    expect(screen.getByTestId("risk-scope")).toHaveTextContent(/foreign/i);
  });

  it("warns, but does not block, when the composed day exceeds the ceiling", async () => {
    vi.spyOn(api, "getSchedule").mockResolvedValue({
      ...VIEW, exceeds_max_day_risk: true, headroom: "-3500.00",
    });
    await renderPanel();
    expect(screen.getByRole("alert")).toHaveTextContent(/RSK-04 will veto entries at fire time/);
  });

  it("adds and deletes rows locally, and sends them on Save", async () => {
    const save = vi.spyOn(api, "saveSchedule").mockResolvedValue({ ...VIEW, config_version: "v4" });
    vi.spyOn(api, "getPreflight").mockResolvedValue({ passed: true, blocked_by: null, checks: [] });
    await renderPanel();

    fireEvent.click(screen.getByText("+ Add entry"));
    fireEvent.change(screen.getByLabelText("time 3"), { target: { value: "13:00" } });
    fireEvent.click(screen.getByLabelText("delete entry 2"));
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(save).toHaveBeenCalled());
    const [rows, maxRisk] = save.mock.calls[0];
    expect(rows.map((r) => r.time)).toEqual(["10:00", "13:00"]);
    expect(maxRisk).toBe("20000");
    expect(await screen.findByText(/Saved as config v4/)).toBeInTheDocument();
  });

  it("an empty cell is sent as empty — the backend inherits the global, not zero", async () => {
    const save = vi.spyOn(api, "saveSchedule").mockResolvedValue({ ...VIEW, config_version: "v4" });
    vi.spyOn(api, "getPreflight").mockResolvedValue({ passed: true, blocked_by: null, checks: [] });
    await renderPanel();

    fireEvent.change(screen.getByLabelText("target premium 1"), { target: { value: "" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][0][0].target_premium).toBe("");
  });

  it("an unset ceiling reads as unknown, never as unlimited", async () => {
    vi.spyOn(api, "getSchedule").mockResolvedValue({
      ...VIEW, max_day_risk: null, headroom: null, exceeds_max_day_risk: false,
    });
    await renderPanel();
    expect(screen.getByTestId("headroom")).toHaveTextContent("no ceiling set");
    expect(screen.queryByText(/unlimited/i)).toBeNull();
  });

  it("the headroom meter fills with the composed day and turns red over the ceiling", async () => {
    const { container, unmount } = render(<SchedulePanel entriesEnabled />);
    await screen.findByRole("table");
    // 14100 of 20000 => ~70%, still green
    const bar = container.querySelector(".meter > i") as HTMLElement;
    expect(bar.style.width).toBe("70.5%");
    expect(container.querySelector(".meter")?.className).not.toContain("over");
    unmount();

    vi.spyOn(api, "getSchedule").mockResolvedValue({
      ...VIEW, day_total_estimate: "23500.00", headroom: "-3500.00", exceeds_max_day_risk: true,
    });
    const second = render(<SchedulePanel entriesEnabled />);
    await screen.findByRole("table");
    // over the ceiling the bar caps at 100% and goes red — it never overflows
    const overBar = second.container.querySelector(".meter > i") as HTMLElement;
    expect(overBar.style.width).toBe("100%");
    expect(second.container.querySelector(".meter")?.className).toContain("over");
  });
});

describe("server-side validation (UI-03)", () => {
  it("marks every offending cell from one 422, not just the first", async () => {
    vi.spyOn(api, "saveSchedule").mockRejectedValue(
      new ApiError(422, {
        errors: [
          { field: "contracts", reason: "out_of_range", index: 0 },
          { field: "stop_loss_pct", reason: "not_in_set", index: 1 },
        ],
      }),
    );
    await renderPanel();
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getByLabelText("contracts 1")).toHaveClass("invalid");
    expect(screen.getByLabelText("stop pct 2")).toHaveClass("invalid");
    // the panel never decided this itself — it rendered what the server said,
    // in operator words (known codes map via REASON_TEXT, unknown pass verbatim)
    expect(screen.getByText(/contracts — outside the allowed range/)).toBeInTheDocument();
  });

  it("renders a schedule-level error (index null) without blaming a row", async () => {
    vi.spyOn(api, "saveSchedule").mockRejectedValue(
      new ApiError(422, { errors: [{ field: "schedule", reason: "empty_schedule", index: null }] }),
    );
    await renderPanel();
    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByText(/schedule — empty_schedule/)).toBeInTheDocument();
  });

  it("the stop-% control offers only the discrete set (STP-02), and never a 'default' option", async () => {
    await renderPanel();
    const options = Array.from(screen.getByLabelText("stop pct 1").querySelectorAll("option"));
    const values = options.map((o) => o.getAttribute("value"));
    expect(values[0]).toBe("95");
    expect(values.at(-1)).toBe("300");
    expect(values).not.toContain("97");
    expect(values).not.toContain("");                     // no empty "inherit" option
    options.forEach((o) => expect(o.textContent).toMatch(/^\d+%$/));
  });

  it("a new row starts at 95%, not blank", async () => {
    const save = vi.spyOn(api, "saveSchedule").mockResolvedValue({ ...VIEW, config_version: "v4" });
    vi.spyOn(api, "getPreflight").mockResolvedValue({ passed: true, blocked_by: null, checks: [] });
    await renderPanel();

    fireEvent.click(screen.getByText("+ Add entry"));
    expect(screen.getByLabelText("stop pct 3")).toHaveValue("95");

    fireEvent.change(screen.getByLabelText("time 3"), { target: { value: "13:00" } });
    fireEvent.click(screen.getByText("Save"));

    // the row is sent with an explicit 95 — the backend would have resolved a blank
    // cell to 95 and echoed it back anyway, so the UI never shows a value it drops
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][0][2].stop_loss_pct).toBe(95);
  });
});

describe("STP-02b / UI-18 — long-recovery buffer ($ stop_rebate_markup)", () => {
  it("renders the saved value and sends an edited one on Save", async () => {
    const save = vi.spyOn(api, "saveSchedule").mockResolvedValue({ ...VIEW, config_version: "v4" });
    vi.spyOn(api, "getPreflight").mockResolvedValue({ passed: true, blocked_by: null, checks: [] });
    await renderPanel();

    const input = screen.getByLabelText("long recovery buffer 1");
    expect(input).toHaveValue("");   // an unset row inherits the global default, never zero

    fireEvent.change(input, { target: { value: "0.30" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][0][0].stop_rebate_markup).toBe("0.30");
  });

  it("an empty cell is sent as empty — inherits the global, not zero", async () => {
    const save = vi.spyOn(api, "saveSchedule").mockResolvedValue({ ...VIEW, config_version: "v4" });
    vi.spyOn(api, "getPreflight").mockResolvedValue({ passed: true, blocked_by: null, checks: [] });
    await renderPanel();

    // explicitly touch the cell and clear it, exactly like the existing
    // target_premium "empty cell" test above — a row VIEW never populated
    // this key for is `undefined`, not `""`, until the operator interacts
    // with the cell at all. Two distinct values (not "" -> "") so the DOM's
    // React value tracker actually fires onChange for the second edit.
    const input = screen.getByLabelText("long recovery buffer 1");
    fireEvent.change(input, { target: { value: "0.10" } });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][0][0].stop_rebate_markup).toBe("");
  });

  it("keeps the dollar worst-case figure permanently visible", async () => {
    // v1.63 UI-18a: the dollar figure is visible whenever markup is
    // set/valid/>0 — BEFORE any focus, hover, or tap.
    vi.spyOn(api, "getSchedule").mockResolvedValue({
      ...VIEW,
      rows: [{ ...VIEW.rows[0], contracts: 1, stop_rebate_markup: "0.30" }, VIEW.rows[1]],
    });
    await renderPanel();
    expect(screen.getByTestId("markup-hint-0")).toHaveTextContent("+$60");
  });

  it("shows the worst-case dollar figure for markup 0.30, 1 contract: +$60", async () => {
    await renderPanel();
    fireEvent.change(screen.getByLabelText("contracts 1"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("long recovery buffer 1"), { target: { value: "0.30" } });
    expect(screen.getByTestId("markup-hint-0")).toHaveTextContent("+$60");
  });

  it("shows the worst-case dollar figure for markup 0.30, 2 contracts: +$120", async () => {
    await renderPanel();   // row 1's own composed contracts is already 2 (VIEW fixture)
    fireEvent.change(screen.getByLabelText("long recovery buffer 1"), { target: { value: "0.30" } });
    expect(screen.getByTestId("markup-hint-0")).toHaveTextContent("+$120");
  });

  it("discloses the UI-18 shortfall sentence alongside the dollar figure", async () => {
    // Presentation ruled by the operator 2026-07-11/2026-07-12 (v1.63
    // UI-18a): the sentence lives behind a styled Tooltip anchored to the
    // dollar figure, never a native title; wording is the spec sentence
    // verbatim.
    await renderPanel();
    fireEvent.change(screen.getByLabelText("long recovery buffer 1"), { target: { value: "0.50" } });
    const sentence = /if the long recovers less than \$0\.50, your net loss exceeds 95% by the shortfall/i;

    // row 1's own composed contracts is 2 -> 0.50 x 100 x 2 x 2 = $200
    expect(screen.getByTestId("markup-hint-0")).toHaveTextContent("+$200");

    fireEvent.click(screen.getByRole("button", { name: "shortfall detail, row 1" }));
    expect(screen.getByTestId("markup-tooltip-0")).toHaveTextContent(sentence);
  });

  it("shortfall tooltip is focus- and tap-capable, never a native title attribute", async () => {
    await renderPanel();
    fireEvent.change(screen.getByLabelText("long recovery buffer 1"), { target: { value: "0.50" } });
    const sentence = /if the long recovers less than \$0\.50, your net loss exceeds 95% by the shortfall/i;

    const trigger = screen.getByRole("button", { name: "shortfall detail, row 1" });
    expect(screen.queryByRole("tooltip")).toBeNull();

    // keyboard focus reveals it
    fireEvent.focus(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent(sentence);
    fireEvent.blur(trigger);
    expect(screen.queryByRole("tooltip")).toBeNull();

    // a pointer tap reveals it too
    fireEvent.click(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent(sentence);

    // never a native title attribute anywhere carries the sentence
    document.querySelectorAll("[title]").forEach((el) => {
      expect(el.getAttribute("title")).not.toMatch(/net loss exceeds/i);
    });
    expect(screen.getByLabelText("long recovery buffer 1")).not.toHaveAttribute("title");
  });

  it("the buffer input's aria-describedby resolves to the sentence even while the tooltip is closed", async () => {
    // Final-review finding (2026-07-12): a keyboard operator tabbed into the
    // input (which does not open the tooltip) must still have the disclosure
    // announced — so the describedby target is an always-present hidden node.
    await renderPanel();
    fireEvent.change(screen.getByLabelText("long recovery buffer 1"), { target: { value: "0.50" } });

    const input = screen.getByLabelText("long recovery buffer 1");
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(screen.queryByRole("tooltip")).toBeNull(); // bubble is closed
    const node = document.getElementById(describedBy as string);
    expect(node).not.toBeNull();
    expect(node).toHaveTextContent(
      /if the long recovers less than \$0\.50, your net loss exceeds 95% by the shortfall/i,
    );
  });

  it("pads a bare '.15' to '0.15' on blur — and never outlines it red", async () => {
    await renderPanel();
    const input = screen.getByLabelText("long recovery buffer 1");
    fireEvent.change(input, { target: { value: ".15" } });
    expect(input).not.toHaveClass("invalid"); // Decimal(".15") is valid backend-side too
    fireEvent.blur(input);
    expect(input).toHaveValue("0.15");
  });

  it("blur never rewrites a genuinely wrong value (reject, never clamp)", async () => {
    await renderPanel();
    const input = screen.getByLabelText("long recovery buffer 1");
    fireEvent.change(input, { target: { value: "0.13" } });
    fireEvent.blur(input);
    expect(input).toHaveValue("0.13");
    expect(input).toHaveClass("invalid");
  });

  it("maps not_strictly_increasing to operator words", async () => {
    await renderPanel();
    vi.spyOn(api, "saveSchedule").mockRejectedValue(
      new ApiError(422, {
        errors: [{ field: "time", reason: "not_strictly_increasing", index: 1 }],
      }),
    );
    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByText(/Row 2: time — must be later than the row above/)).toBeTruthy();
  });

  it("shows nothing when the buffer is zero or blank (UI-18: only discloses when markup > 0)", async () => {
    await renderPanel();
    expect(screen.queryByTestId("markup-hint-0")).toBeNull();

    fireEvent.change(screen.getByLabelText("long recovery buffer 1"), { target: { value: "0.00" } });
    expect(screen.queryByTestId("markup-hint-0")).toBeNull();
  });

  it("rejects an invalid step client-side (reject, never clamp) and shows the range/step hint", async () => {
    await renderPanel();
    const input = screen.getByLabelText("long recovery buffer 1");

    fireEvent.change(input, { target: { value: "0.13" } });
    expect(input).toHaveClass("invalid");
    expect(input).toHaveValue("0.13");   // never silently rewritten/clamped
    expect(screen.getByTestId("markup-hint-0")).toHaveTextContent("$0.00–$5.00, $0.05 steps");
  });

  it("rejects an out-of-range value client-side", async () => {
    await renderPanel();
    const input = screen.getByLabelText("long recovery buffer 1");
    fireEvent.change(input, { target: { value: "5.05" } });
    expect(input).toHaveClass("invalid");
  });

  it("marks the cell from a server-side 422 too", async () => {
    await renderPanel();
    vi.spyOn(api, "saveSchedule").mockRejectedValue(
      new ApiError(422, {
        errors: [{ field: "stop_rebate_markup", reason: "out_of_range", index: 0 }],
      }),
    );
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(screen.getByLabelText("long recovery buffer 1")).toHaveClass("invalid"),
    );
  });
});

describe("UC-02 pre-flight checklist", () => {
  it("shows pass/fail per item after a save", async () => {
    vi.spyOn(api, "saveSchedule").mockResolvedValue({ ...VIEW, config_version: "v4" });
    vi.spyOn(api, "getPreflight").mockResolvedValue(PREFLIGHT);
    await renderPanel();
    fireEvent.click(screen.getByText("Save"));

    const list = await screen.findByTestId("preflight");
    expect(list).toHaveTextContent("schedule");
    expect(list).toHaveTextContent("ENT-01a");
    expect(list).toHaveTextContent("mismatch open");
    expect(list.querySelectorAll("li.pass")).toHaveLength(1);
    expect(list.querySelectorAll("li.fail")).toHaveLength(1);
  });
});

describe("ENT-09 manual fire (UI-22)", () => {
  it("disables ▶ when entries are not enabled", async () => {
    await renderPanel(false);
    expect(screen.getByLabelText("fire entry 1")).toBeDisabled();
    expect(screen.getByLabelText("fire entry 1")).toHaveAttribute(
      "title", expect.stringContaining("Blocked"),
    );
  });

  it("submits nothing until OK is pressed", async () => {
    const preview = vi.spyOn(api, "firePreview").mockResolvedValue(PREVIEW);
    const fire = vi.spyOn(api, "fire");
    await renderPanel();

    fireEvent.click(screen.getByLabelText("fire entry 1"));
    await screen.findByRole("dialog");
    expect(preview).toHaveBeenCalledWith(1);
    expect(fire).not.toHaveBeenCalled();          // opening the dialog is not firing

    fireEvent.click(screen.getByText("Cancel"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(fire).not.toHaveBeenCalled();          // cancel means no order
  });

  it("shows the worst case LABELLED as an estimate, naming RSK-04 as authoritative", async () => {
    vi.spyOn(api, "firePreview").mockResolvedValue(PREVIEW);
    await renderPanel();
    fireEvent.click(screen.getByLabelText("fire entry 1"));

    await screen.findByRole("dialog");
    const box = screen.getByTestId("fire-estimate");
    expect(box).toHaveTextContent(/Worst case \(ESTIMATE\)/);
    expect(box).toHaveTextContent("$9400.00");
    expect(screen.getByText(/\(width - target premium\) x 100 x contracts/)).toBeInTheDocument();
    expect(screen.getByText(/RSK-04 check runs on the real strikes and may still\s+veto/)).toBeInTheDocument();
  });

  it("OK fires with the press_id from the preview, so a double-click is one attempt", async () => {
    vi.spyOn(api, "firePreview").mockResolvedValue(PREVIEW);
    const fire = vi.spyOn(api, "fire").mockResolvedValue({
      result: "filled", entry_id: "d#1", initiator: "manual_entry",
    });
    await renderPanel();

    fireEvent.click(screen.getByLabelText("fire entry 1"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("OK"));

    await waitFor(() => expect(fire).toHaveBeenCalledWith(1, "press-abc"));
    expect(await screen.findByText(/filled \(manual_entry\)/)).toBeInTheDocument();
  });

  it("renders a refusal with the backend's own reason", async () => {
    vi.spyOn(api, "firePreview").mockResolvedValue(PREVIEW);
    vi.spyOn(api, "fire").mockResolvedValue({ result: "skipped", reason: "max_day_risk" });
    await renderPanel();

    fireEvent.click(screen.getByLabelText("fire entry 1"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("OK"));

    expect(await screen.findByText(/skipped — max_day_risk/)).toBeInTheDocument();
  });

  it("fires the row that was pressed, not the first row", async () => {
    vi.spyOn(api, "firePreview").mockResolvedValue({ ...PREVIEW, entry_number: 2, press_id: "press-2" });
    const fire = vi.spyOn(api, "fire").mockResolvedValue({ result: "filled" });
    await renderPanel();

    fireEvent.click(screen.getByLabelText("fire entry 2"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByText("OK"));

    await waitFor(() => expect(fire).toHaveBeenCalledWith(2, "press-2"));
  });
});

describe("entry time — local equivalent + military/market-hours hints", () => {
  it("shows the ET time's local equivalent under a valid in-hours time", async () => {
    await renderPanel();
    // row 1 loads as 10:00 ET (in hours) -> a local-equivalent hint (≈ HH:MM),
    // whatever the runner's timezone is
    expect(screen.getAllByTestId("time-hint")[0].textContent).toMatch(/≈\s*\d{2}:\d{2}/);
  });

  it("accepts a UK-style dot separator (11.53 shows a local echo, not an error)", async () => {
    await renderPanel();
    fireEvent.change(screen.getByLabelText("time 1"), { target: { value: "11.53" } });
    expect(screen.getAllByTestId("time-hint")[0].textContent).toMatch(/≈\s*\d{2}:\d{2}/);
  });

  it("flags a non-military (am/pm) time", async () => {
    await renderPanel();
    fireEvent.change(screen.getByLabelText("time 1"), { target: { value: "1:53pm" } });
    expect(screen.getAllByTestId("time-hint")[0].textContent).toMatch(/24-hour HH:MM/);
  });

  it("flags a valid time that falls outside market hours (09:30–16:00 ET)", async () => {
    await renderPanel();
    fireEvent.change(screen.getByLabelText("time 1"), { target: { value: "08:00" } });
    expect(screen.getAllByTestId("time-hint")[0].textContent).toMatch(/outside market hours/);
  });
});
