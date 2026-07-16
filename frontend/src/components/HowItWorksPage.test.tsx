// DOC-01..05 (doc 12, slice 4) -- the How-it-works tab renders the ratified
// guide FROM THE BACKEND'S OWN READ of spec/12-how-it-works.md (DOC-05
// single source, GET /guide) -- these tests pin what it renders off that
// read model, never a frontend copy of the prose, and that no trading
// capability rides along on this tab (UI-03/DOC-05: read-only).
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import mermaid from "mermaid";

import { api } from "../api";
import type { GuideData } from "../types";
import { HowItWorksPage } from "./HowItWorksPage";

// DOC-04's flowchart rendering is pinned narrowly here: that the mermaid
// fenced code block is intercepted and handed to mermaid's render API,
// never left as raw source text -- via this lightweight fake, not the real
// diagramming engine (which needs real-browser canvas/SVG measurement jsdom
// can't fully provide).
vi.mock("mermaid", () => ({
  default: { initialize: vi.fn(), render: vi.fn() },
}));

function guideFixture(overrides: Partial<GuideData> = {}): GuideData {
  return {
    guide_markdown: [
      "# THE GUIDE (ratified content, v1.72 — describes spec v1.72; DOC-05 stamp)",
      "",
      "## The master flowchart",
      "",
      "```mermaid",
      "flowchart TD",
      "    A --> B",
      "```",
      "",
      "## 1. What the bot trades, and the shape of the trade",
      "",
      "Body text with the **house example** and a `wing_width` mention.",
      "",
      "## 2. Setting up a day",
      "",
      "More body text.",
      "",
      "## 10. The calendar",
      "",
      "Final chapter body.",
      "",
    ].join("\n"),
    guide_version: "1.72",
    running_spec_version: "1.72",
    version_mismatch: false,
    version_unknown: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
  // vitest.config.ts's `restoreMocks: true` calls .mockRestore() on every
  // mock before each test, which clears a plain vi.fn()'s implementation
  // back to a no-op (there is no "original" to restore for a module mock,
  // unlike a vi.spyOn) -- so the fake mermaid.render must be re-armed here,
  // every test, rather than once at module scope.
  vi.mocked(mermaid.render).mockResolvedValue(
    { svg: '<svg data-testid="fake-flowchart-svg"></svg>' } as Awaited<ReturnType<typeof mermaid.render>>,
  );
});

describe("HowItWorksPage — DOC-05 single-source rendering", () => {
  it("renders the guide fetched from GET /guide, stamped with its own version", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    expect(await screen.findByTestId("guide-version-stamp")).toHaveTextContent("v1.72");
    expect(screen.getByRole("heading", { name: /What the bot trades, and the shape of the trade/ }))
      .toBeInTheDocument();
    expect(screen.queryByTestId("guide-mismatch-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("how-it-works-placeholder")).not.toBeInTheDocument();
  });

  it("banners a stamped-vs-running version mismatch instead of pretending currency (DOC-05)", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(
      guideFixture({ guide_version: "1.72", running_spec_version: "1.90", version_mismatch: true }));
    render(<HowItWorksPage />);

    const banner = await screen.findByTestId("guide-mismatch-banner");
    expect(banner).toHaveTextContent(/v1\.72/);
    expect(banner).toHaveTextContent(/v1\.90/);
  });

  it("does not banner when the stamp matches the running build (not a tautology)", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    await screen.findByTestId("guide-version-stamp");
    expect(screen.queryByTestId("guide-mismatch-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("guide-unknown-banner")).not.toBeInTheDocument();
  });

  it("banners an UNPARSEABLE guide stamp as 'cannot verify' — fails toward showing (DOC-05)", async () => {
    // The guide's own "describes spec vX.YY" stamp failed to parse server-
    // side: the comparison is unverifiable, and the banner must say so
    // rather than silently reading as verified currency.
    vi.spyOn(api, "getGuide").mockResolvedValue(
      guideFixture({ guide_version: null, version_mismatch: false, version_unknown: true }));
    render(<HowItWorksPage />);

    const banner = await screen.findByTestId("guide-unknown-banner");
    expect(banner).toHaveTextContent(/cannot verify/i);
    expect(banner).toHaveTextContent(/guide's own version stamp/i);
  });

  it("banners an unreadable RUNNING spec version as 'cannot verify' too (DOC-05)", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(
      guideFixture({ running_spec_version: null, version_mismatch: false, version_unknown: true }));
    render(<HowItWorksPage />);

    const banner = await screen.findByTestId("guide-unknown-banner");
    expect(banner).toHaveTextContent(/cannot verify/i);
    expect(banner).toHaveTextContent(/running build's spec version/i);
  });

  it("renders a chapter table of contents from the guide's own ## headings", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    const toc = await screen.findByTestId("guide-toc");
    expect(within(toc).getByRole("button", { name: /What the bot trades/ })).toBeInTheDocument();
    expect(within(toc).getByRole("button", { name: "2. Setting up a day" })).toBeInTheDocument();
    expect(within(toc).getByRole("button", { name: /The master flowchart/ })).toBeInTheDocument();
  });

  it("clicking a TOC entry scrolls to its chapter without touching the SPA's own hash router", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    const scrollSpy = vi.fn();
    // jsdom does not implement scrollIntoView.
    Element.prototype.scrollIntoView = scrollSpy;
    window.location.hash = "";
    render(<HowItWorksPage />);

    const toc = await screen.findByTestId("guide-toc");
    await userEvent.click(within(toc).getByRole("button", { name: "2. Setting up a day" }));

    expect(scrollSpy).toHaveBeenCalled();
    // A real href="#chapter-N" link would have changed window.location.hash,
    // which the SPA's own router (router.ts) treats as page navigation --
    // this must never happen from an in-page TOC click.
    expect(window.location.hash).toBe("");
  });

  it("renders the master flowchart through mermaid, never as raw fenced source", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    const flowchart = await screen.findByTestId("guide-flowchart");
    await waitFor(() =>
      expect(within(flowchart).queryByTestId("fake-flowchart-svg")).toBeInTheDocument());
    expect(screen.queryByText("flowchart TD")).not.toBeInTheDocument();
    // Opus final-review NIT: the mdComponents `pre` unwrap — the flowchart
    // wrapper (and its error fallback) must not sit inside a UA-styled <pre>
    // leaking monospace/pre-whitespace onto it.
    expect(flowchart.closest("pre")).toBeNull();
  });

  it("the master flowchart is clickable to a full-screen pannable zoomable view (DOC-05, v1.77)", async () => {
    // TC-DOC-01's v1.77 zoom scenario: "the operator literally cannot read
    // the current rendering; usability defect, not polish". This pins the
    // shared ZoomableFigure being wired to the flowchart specifically (the
    // component's own generic pan/zoom behavior is pinned once in
    // ZoomableFigure.test.tsx, not repeated here).
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    const flowchart = await screen.findByTestId("guide-flowchart");
    await waitFor(() =>
      expect(within(flowchart).queryByTestId("fake-flowchart-svg")).toBeInTheDocument());

    const trigger = screen.getByRole("button", { name: /master flowchart.*click to enlarge, pan and zoom/i });
    expect(trigger).toBeInTheDocument();

    await userEvent.click(trigger);
    const overlay = await screen.findByTestId("zoom-overlay");
    expect(overlay).toHaveAttribute("role", "dialog");
    expect(overlay).toHaveAttribute("aria-modal", "true");

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("zoom-overlay")).not.toBeInTheDocument();
  });

  it("every DOC-03 chapter present in the fixture appears as its own heading", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    expect(await screen.findByRole("heading", { name: /1\. What the bot trades/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /2\. Setting up a day/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /10\. The calendar/ })).toBeInTheDocument();
  });

  it("carries no trading controls (DOC-05 read-only tab)", async () => {
    vi.spyOn(api, "getGuide").mockResolvedValue(guideFixture());
    render(<HowItWorksPage />);

    await screen.findByTestId("guide-version-stamp");
    expect(screen.queryByRole("button", { name: /^close$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /flatten/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^arm$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /fire/i })).not.toBeInTheDocument();
  });

  it("shows a load error, never a blank page, when GET /guide fails", async () => {
    vi.spyOn(api, "getGuide").mockRejectedValue(new Error("500"));
    render(<HowItWorksPage />);

    expect(await screen.findByText(/could not load the guide/i)).toBeInTheDocument();
  });
});
