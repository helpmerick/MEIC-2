// DOC-06/UI-32 (doc 12, slice 6, v1.78) -- the Getting-started tab renders
// the ratified "# GETTING STARTED" section FROM THE BACKEND'S OWN READ of
// spec/12-how-it-works.md (DOC-05 single source, GET /getting-started).
// These tests pin TC-DOC-01's no-secret-leak scenario on the rendered side:
// every template variable NAME renders literally, while NOTHING value-shaped
// ever does -- plus the tab's own DOC-05 stamp/banner discipline (its OWN
// v1.78 stamp, never the sibling guide section's v1.72 one) and that no
// trading capability rides along on this read-only tab (UI-03).
//
// The backend halves -- the payload being byte-for-byte the hash-locked spec
// section's text, the planted-.env-sentinel never leaking, the two-section
// boundary -- are pinned in tests/adapters/test_api_getting_started.py.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "../api";
import type { GettingStartedData } from "../types";
import { GettingStartedPage } from "./GettingStartedPage";

// A faithful extract of the ratified v1.78 section: the real heading/stamp,
// all five DOC-06 sections, and the annotated `.env` template as the GFM
// table the spec ships (names and where-to-obtain guidance ONLY).
const SECTION_MARKDOWN = [
  "# GETTING STARTED (ratified content, v1.78 — describes spec v1.78 and the build's true run procedure; DOC-05 stamp)",
  "",
  "## 1. Prerequisites, and how this build actually runs",
  "",
  "**Python 3.11**, with the project's own virtual environment (`.venv`).",
  "",
  "## 2. The `.env` file — names and where to get them, never values",
  "",
  "| Variable name | What it is | Where you get it |",
  "|---|---|---|",
  "| `MEIC_USER_PASSWORD` | The password that unlocks this control panel. | You choose this yourself. |",
  "| `TT_CERT_*` [the literal names: `TT_CERT_PROVIDER_SECRET`, `TT_CERT_REFRESH_TOKEN`, `TT_CERT_ACCOUNT`] | Sandbox credentials. | tastytrade's OAuth application settings. |",
  "| `TT_PROD_*` [the literal names: `TT_PROD_PROVIDER_SECRET`, `TT_PROD_REFRESH_TOKEN`, `TT_PROD_ACCOUNT`] | Production credentials. | The same OAuth settings, production side. |",
  "| `MEIC_LIVE_IS_TEST` | The first of the two deliberate switches. | You set this yourself. |",
  "| `MEIC_ALLOW_PRODUCTION` | The second deliberate switch. | Same as above. |",
  "| *(data directory location)* [`MEIC_DATA_DIR` — defaults to `data/`] | Where the bot keeps its durable record. | Chosen by you. |",
  "",
  "## 3. The numbers live mode refuses to trade without",
  "",
  "- **`max_day_risk`** — the absolute dollar ceiling.",
  "",
  "## 4. The paper-first first-run sequence",
  "",
  "1. Start the **paper** build.",
  "",
  "## 5. Going live — the two-switch ritual, and a plain warning",
  "",
  "Live means live.",
  "",
].join("\n");

const TEMPLATE_NAMES = [
  "MEIC_USER_PASSWORD",
  "TT_CERT_PROVIDER_SECRET", "TT_CERT_REFRESH_TOKEN", "TT_CERT_ACCOUNT",
  "TT_PROD_PROVIDER_SECRET", "TT_PROD_REFRESH_TOKEN", "TT_PROD_ACCOUNT",
  "MEIC_LIVE_IS_TEST", "MEIC_ALLOW_PRODUCTION", "MEIC_DATA_DIR",
];

function fixture(overrides: Partial<GettingStartedData> = {}): GettingStartedData {
  return {
    getting_started_markdown: SECTION_MARKDOWN,
    getting_started_version: "1.78",
    running_spec_version: "1.78",
    version_mismatch: false,
    version_unknown: false,
    ...overrides,
  };
}

/** Every rendered text node, joined with a SPACE separator — textContent
 * alone concatenates adjacent nodes (e.g. table cells) with no separator,
 * which could weld two legitimate short strings into one long token-shaped
 * false positive; a real leaked secret lives inside a single text node. */
function renderedText(): string {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const parts: string[] = [];
  while (walker.nextNode()) parts.push(walker.currentNode.textContent ?? "");
  return parts.join(" ");
}

describe("GettingStartedPage — DOC-06/UI-32 fifth tab", () => {
  it("renders the section fetched from GET /getting-started, stamped with its OWN version", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);

    // v1.78 — THIS section's own stamp, never the guide's sibling v1.72 one.
    expect(await screen.findByTestId("getting-started-version-stamp")).toHaveTextContent("v1.78");
    expect(screen.getByRole("heading", { name: /Prerequisites, and how this build actually runs/ }))
      .toBeInTheDocument();
    expect(screen.queryByTestId("getting-started-mismatch-banner")).not.toBeInTheDocument();
  });

  it("banners a stamped-vs-running version mismatch instead of pretending currency (DOC-05)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(
      fixture({ getting_started_version: "1.78", running_spec_version: "1.90", version_mismatch: true }));
    render(<GettingStartedPage />);

    const banner = await screen.findByTestId("getting-started-mismatch-banner");
    expect(banner).toHaveTextContent(/v1\.78/);
    expect(banner).toHaveTextContent(/v1\.90/);
  });

  it("does not banner when its own stamp matches the running build (not a tautology)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);

    await screen.findByTestId("getting-started-version-stamp");
    expect(screen.queryByTestId("getting-started-mismatch-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("getting-started-unknown-banner")).not.toBeInTheDocument();
  });

  it("banners an UNPARSEABLE section stamp as 'cannot verify' — fails toward showing (DOC-05)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(
      fixture({ getting_started_version: null, version_mismatch: false, version_unknown: true }));
    render(<GettingStartedPage />);

    const banner = await screen.findByTestId("getting-started-unknown-banner");
    expect(banner).toHaveTextContent(/cannot verify/i);
    expect(banner).toHaveTextContent(/section's own version stamp/i);
  });

  it("banners an unreadable RUNNING spec version as 'cannot verify' too (DOC-05)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(
      fixture({ running_spec_version: null, version_mismatch: false, version_unknown: true }));
    render(<GettingStartedPage />);

    const banner = await screen.findByTestId("getting-started-unknown-banner");
    expect(banner).toHaveTextContent(/cannot verify/i);
    expect(banner).toHaveTextContent(/running build's spec version/i);
  });

  // --- TC-DOC-01: "Getting-started never leaks a secret (DOC-06/UI-32)" ------

  it("renders variable NAMES and where-to-obtain guidance only (DOC-06/UI-32)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);
    await screen.findByTestId("getting-started-version-stamp");

    // Every template variable NAME renders literally...
    const text = renderedText();
    for (const name of TEMPLATE_NAMES) expect(text).toContain(name);
    // ...inside the annotated template rendered as a real table (remark-gfm),
    // whose columns are the DOC-06 contract: name / what it is / where you
    // get it — guidance columns, no "current value" column anywhere.
    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Variable name" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Where you get it" })).toBeInTheDocument();
    expect(within(table).queryByRole("columnheader", { name: /current value/i })).not.toBeInTheDocument();
  });

  it("never renders a value-shaped secret anywhere in the tab (DOC-06/UI-32)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);
    await screen.findByTestId("getting-started-version-stamp");

    const text = renderedText();
    // No template name is ever paired with a value (NAME=value / NAME: value).
    expect(text).not.toMatch(
      /(TT_CERT_\w+|TT_PROD_\w+|MEIC_USER_PASSWORD|MEIC_LIVE_IS_TEST|MEIC_ALLOW_PRODUCTION|MEIC_DATA_DIR)\s*[=:]\s*\S+/);
    // No token-shaped run anywhere: 28+ consecutive credential-alphabet
    // chars (real provider secrets / refresh tokens are 32+; the longest
    // legitimate run in the ratified prose is well under 28 — calibrated
    // against the real section text, same bar as the backend pin).
    expect(text).not.toMatch(/[A-Za-z0-9+/_-]{28,}/);
  });

  it("all five DOC-06 sections are present (the completeness contract)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);

    expect(await screen.findByRole("heading", { name: /1\. Prerequisites/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /2\. The \.env file/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /3\. The numbers live mode refuses to trade without/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /4\. The paper-first first-run sequence/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /5\. Going live/ })).toBeInTheDocument();
  });

  // --- shared tab discipline, mirroring HowItWorksPage's own pins ------------

  it("renders a section table of contents from the section's own ## headings", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);

    const toc = await screen.findByTestId("getting-started-toc");
    expect(within(toc).getByRole("button", { name: /1\. Prerequisites/ })).toBeInTheDocument();
    expect(within(toc).getByRole("button", { name: /5\. Going live/ })).toBeInTheDocument();
  });

  it("clicking a TOC entry scrolls to its section without touching the SPA's own hash router", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    const scrollSpy = vi.fn();
    Element.prototype.scrollIntoView = scrollSpy; // jsdom lacks scrollIntoView
    window.location.hash = "";
    render(<GettingStartedPage />);

    const toc = await screen.findByTestId("getting-started-toc");
    await userEvent.click(within(toc).getByRole("button", { name: /4\. The paper-first/ }));

    expect(scrollSpy).toHaveBeenCalled();
    expect(window.location.hash).toBe("");
  });

  it("carries no trading controls (read-only tab)", async () => {
    vi.spyOn(api, "getGettingStarted").mockResolvedValue(fixture());
    render(<GettingStartedPage />);

    await screen.findByTestId("getting-started-version-stamp");
    expect(screen.queryByRole("button", { name: /^close$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /flatten/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^arm$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /fire/i })).not.toBeInTheDocument();
  });

  it("shows a load error, never a blank page, when GET /getting-started fails", async () => {
    vi.spyOn(api, "getGettingStarted").mockRejectedValue(new Error("500"));
    render(<GettingStartedPage />);

    expect(await screen.findByText(/could not load the getting-started content/i)).toBeInTheDocument();
  });
});
