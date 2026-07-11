import { describe, expect, it } from "vitest";
import {
  contractDollars,
  contractDollarsPlain,
  contractDollarsValue,
  formatDollars,
  isValidStopRebateMarkup,
  normalizeMoneyInput,
  stopRebateMarkupWorstCase,
} from "./money";

// Operator request 2026-07-11: SPX options carry a $100 multiplier, so a
// per-share premium Decimal string must be shifted two places (never
// float-multiplied — 5.2 * 100 !== 520 exactly in IEEE754) and scaled by the
// entry's own contracts count.
describe("contractDollars — string-shift exactness", () => {
  it("shifts a plain 2dp credit exactly", () => {
    expect(contractDollars("5.20")).toBe("+$520");
  });

  it("shifts a small premium exactly (the classic float-multiply failure case)", () => {
    // Number("0.40") * 100 === 40.00000000000001 in plain floating point —
    // this must come out exact.
    expect(contractDollars("0.40")).toBe("+$40");
  });

  it("handles a whole-dollar (no fractional part) premium", () => {
    expect(contractDollars("4")).toBe("+$400");
  });

  it("signs a negative premium correctly, never double-signing", () => {
    expect(contractDollars("-1.05")).toBe("-$105");
  });

  it("treats a zero premium as non-negative (matches the codebase's money() convention)", () => {
    expect(contractDollars("0.00")).toBe("+$0");
    expect(contractDollars("-0.00")).toBe("+$0");
  });

  it("preserves sub-cent precision in the source Decimal rather than rounding it away", () => {
    expect(contractDollars("5.205")).toBe("+$520.5");
    expect(contractDollars("-0.001")).toBe("-$0.1");
  });

  it("scales by contracts > 1 exactly", () => {
    expect(contractDollars("2.24", 3)).toBe("+$672");
    expect(contractDollars("0.40", 10)).toBe("+$400");
  });

  it("matches the worked example from the operator's request: a $5.20 credit split $2.24/$2.96 across two sides", () => {
    expect(contractDollars("2.24")).toBe("+$224");
    expect(contractDollars("2.96")).toBe("+$296");
    expect(contractDollars("5.20")).toBe("+$520");
  });
});

describe("contractDollarsPlain — unsigned magnitude for existing literal $ templates", () => {
  it("drops the sign and the $ (caller supplies its own $)", () => {
    expect(contractDollarsPlain("5.20")).toBe("520");
  });

  it("still shows the magnitude of a negative value (unsigned by design)", () => {
    expect(contractDollarsPlain("-1.05")).toBe("105");
  });

  it("scales by contracts", () => {
    expect(contractDollarsPlain("4.00", 3)).toBe("1200");
  });
});

describe("contractDollarsValue — numeric aggregation input", () => {
  it("returns the exact scaled number", () => {
    expect(contractDollarsValue("5.20")).toBe(520);
    expect(contractDollarsValue("0.40", 3)).toBe(120);
    expect(contractDollarsValue("-1.05")).toBe(-105);
  });
});

describe("formatDollars — rendering a summed aggregate", () => {
  it("formats a positive whole-dollar total without forced cents", () => {
    expect(formatDollars(520)).toBe("+$520");
  });

  it("formats a negative total", () => {
    expect(formatDollars(-105)).toBe("-$105");
  });

  it("formats zero as non-negative", () => {
    expect(formatDollars(0)).toBe("+$0");
    expect(formatDollars(-0)).toBe("+$0");
  });

  it("keeps genuine sub-dollar precision while trimming a trailing .00", () => {
    expect(formatDollars(520.5)).toBe("+$520.5");
  });

  it("cleans float summation noise (e.g. 224 + 296 computed via several additions)", () => {
    const sum = contractDollarsValue("2.24") + contractDollarsValue("2.96");
    expect(formatDollars(sum)).toBe("+$520");
  });
});

// STP-02b / UI-18: $0.00-$5.00, step $0.05 (doc 06 §60).
describe("isValidStopRebateMarkup — range + step, reject never clamp", () => {
  it("accepts blank — inherits the global default, never treated as zero", () => {
    expect(isValidStopRebateMarkup("")).toBe(true);
    expect(isValidStopRebateMarkup("   ")).toBe(true);
  });

  it("accepts the low and high edges", () => {
    expect(isValidStopRebateMarkup("0.00")).toBe(true);
    expect(isValidStopRebateMarkup("5.00")).toBe(true);
  });

  it("accepts every legal $0.05 step, not just round numbers", () => {
    expect(isValidStopRebateMarkup("0.05")).toBe(true);
    expect(isValidStopRebateMarkup("0.30")).toBe(true);
    expect(isValidStopRebateMarkup("4.95")).toBe(true);
  });

  it("rejects a step that isn't a multiple of $0.05", () => {
    expect(isValidStopRebateMarkup("0.13")).toBe(false);
    expect(isValidStopRebateMarkup("0.01")).toBe(false);
    // the classic float trap: 0.15 % 0.05 !== 0 in IEEE754 — must still pass
    expect(isValidStopRebateMarkup("0.15")).toBe(true);
  });

  it("rejects out-of-range values", () => {
    expect(isValidStopRebateMarkup("-0.05")).toBe(false);
    expect(isValidStopRebateMarkup("5.05")).toBe(false);
    expect(isValidStopRebateMarkup("10.00")).toBe(false);
  });

  it("rejects unparsable input", () => {
    expect(isValidStopRebateMarkup("abc")).toBe(false);
    expect(isValidStopRebateMarkup("0.05.05")).toBe(false);
    expect(isValidStopRebateMarkup(".")).toBe(false);
  });

  it("accepts a bare leading dot — '.15' IS 0.15 (backend Decimal agrees)", () => {
    expect(isValidStopRebateMarkup(".15")).toBe(true);
    expect(isValidStopRebateMarkup(".13")).toBe(false); // still a bad step
  });

  it("rejects genuine sub-cent precision (the field's step is whole nickels)", () => {
    expect(isValidStopRebateMarkup("0.301")).toBe(false);
  });
});

describe("stopRebateMarkupWorstCase — mirrors domain/stop_policy.py's markup_worst_case_increase", () => {
  it("markup 0.30, 1 contract: +$60", () => {
    expect(stopRebateMarkupWorstCase("0.30", 1)).toBe("60");
  });

  it("markup 0.30, 2 contracts: +$120", () => {
    expect(stopRebateMarkupWorstCase("0.30", 2)).toBe("120");
  });

  it("defaults to 1 contract when omitted", () => {
    expect(stopRebateMarkupWorstCase("0.30")).toBe("60");
  });

  it("zero markup is zero worst case", () => {
    expect(stopRebateMarkupWorstCase("0.00", 3)).toBe("0");
  });

  it("the high edge, several contracts, exact", () => {
    expect(stopRebateMarkupWorstCase("5.00", 4)).toBe("4000");
  });
});

describe("normalizeMoneyInput — blur formatter, never a clamp", () => {
  it("pads the missing leading zero", () => {
    expect(normalizeMoneyInput(".15")).toBe("0.15");
  });

  it("pads bare integers and trailing dots to two decimals", () => {
    expect(normalizeMoneyInput("5")).toBe("5.00");
    expect(normalizeMoneyInput("5.")).toBe("5.00");
    expect(normalizeMoneyInput("0.3")).toBe("0.30");
  });

  it("strips redundant leading zeros", () => {
    expect(normalizeMoneyInput("00.30")).toBe("0.30");
  });

  it("keeps sub-cent digits so validation still sees them", () => {
    expect(normalizeMoneyInput("0.301")).toBe("0.301");
  });

  it("returns non-decimal shapes and blanks unchanged", () => {
    expect(normalizeMoneyInput("abc")).toBe("abc");
    expect(normalizeMoneyInput("")).toBe("");
    expect(normalizeMoneyInput(".")).toBe(".");
  });
});
