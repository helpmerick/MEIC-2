import { describe, expect, it } from "vitest";
import { contractDollars, contractDollarsPlain, contractDollarsValue, formatDollars } from "./money";

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
