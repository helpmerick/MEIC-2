// UI-31 (v1.73, queue slice 5) completeness gate — TC-UI-09 scenario 3:
// "An unexplained event type is a test failure ... Then the suite fails
// naming the event type." This test does not re-implement or duplicate the
// backend's own list of renderable event types: it WALKS the real authority
// — adapters/api/app.py's `_describe` `table` dict — by reading that exact
// source file, so a new event type added there with no matching entry in
// ACTIVITY_VOCABULARY fails THIS test, by name, the moment it lands.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ACTIVITY_VOCABULARY } from "./activityVocabulary";

// NOTE: deliberately path.resolve(dirname, ...) rather than
// `new URL("../../backend/...", import.meta.url)` — Vite statically
// rewrites that exact literal-relative-path-plus-import.meta.url pattern
// into an asset-import URL (its "new URL(..., import.meta.url)" asset
// convention), which under vitest's dev server resolves to an
// `http://.../@fs/...` URL instead of a real `file://` one and breaks
// fileURLToPath. Computing __dirname ourselves first avoids that rewrite.
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const APP_PY = path.resolve(__dirname, "../../backend/src/meic/adapters/api/app.py");

/** Every event-type key the backend's `_describe` table can render, read
 * straight from its own source text. Throws loudly (never returns an empty/
 * partial list silently) if the table's shape ever changes so much this
 * regex can no longer find it — a silently-empty extraction would make this
 * whole completeness gate a no-op without anyone noticing. */
function renderableEventTypes(): string[] {
  const src = readFileSync(APP_PY, "utf-8");
  const tableMatch = /table:\s*dict\[str,\s*tuple\[str,\s*str\]\]\s*=\s*\{([\s\S]*?)\n\s*\}/.exec(src);
  if (!tableMatch) {
    throw new Error(
      "could not locate _describe's event `table` dict in adapters/api/app.py — " +
        "has it moved, been renamed, or changed shape? This completeness gate " +
        "cannot enforce anything until it can find the real table again."
    );
  }
  const keys = [...tableMatch[1].matchAll(/"([A-Za-z0-9_]+)":/g)].map((m) => m[1]);
  if (keys.length === 0) {
    throw new Error("matched the _describe table block but extracted zero event-type keys");
  }
  return keys;
}

describe("Activity feed tooltip completeness (UI-31, TC-UI-09 scenario 3)", () => {
  it("every event type the backend can render has a plain-English tooltip explanation", () => {
    const missing = renderableEventTypes().filter((t) => !(t in ACTIVITY_VOCABULARY));
    expect(missing, `event type(s) renderable by the feed with no tooltip explanation: ${missing.join(", ")}`)
      .toEqual([]);
  });
});
