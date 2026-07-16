// DOC-01..05/UI-29 (v1.72, doc 12) — the ratified How-it-works tab. DOC-05
// (single source): this renders the guide FROM spec/12-how-it-works.md
// ITSELF, fetched fresh from the backend on every mount — never a frontend
// copy of the prose that could drift out of ratification. The backend
// (GET /guide, adapters/api/app.py) reads the file straight off disk, splits
// it at the "# THE GUIDE" heading, and reports the guide's own "describes
// spec vX.YY" stamp alongside the RUNNING build's own spec version (spec/
// README.md's changelog head) — this component only renders that comparison,
// it never re-derives "what version is this build" itself (DOC-05).
//
// DOC-01: the guide's prose is ratified content — it is rendered VERBATIM.
// The only presentational liberties taken here (flagged in the build report):
//   1. The guide's own leading "# THE GUIDE (...)" markdown heading is
//      rendered as an <h2>, not <h1> — so the page has exactly one <h1>
//      ("How it works", matching every other tab's own page title). The text
//      itself is untouched, only the HTML heading LEVEL changes.
//   2. The chapter table of contents scrolls the reader to an in-page
//      anchor via scrollIntoView, rather than a real `href="#chapter-N"`
//      link — this SPA's own router (router.ts) treats `window.location.hash`
//      as page navigation (UI-27), so a plain hash-changing link here would
//      be intercepted by the app's own hashchange listener and bounce the
//      operator back to the Trading tab instead of scrolling.
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import { api } from "../api";
import type { GuideData } from "../types";
import { ZoomableFigure } from "./ZoomableFigure";

// slugify/flattenText/buildToc/scrollToChapter/MermaidDiagram are exported
// for GettingStartedPage.tsx (DOC-06/UI-32, doc 12 slice 6), which mirrors
// this page's DOC-05 rendering over spec/12's OWN "# GETTING STARTED"
// section — shared here rather than duplicated so the two tabs can never
// drift apart in how they slug headings or render a ratified diagram.
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

// react-markdown hands headings/code their rendered children, which is
// almost always a single string here (the guide's headings and inline code
// spans carry no nested markup) — this flattens whatever shape shows up
// (string, number, array of the same) into plain text for slugging.
export function flattenText(node: unknown): string {
  if (node === null || node === undefined) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flattenText).join("");
  return "";
}

export interface TocEntry {
  id: string;
  title: string;
}

/** DOC-02/04: a small table of contents from the guide's own `##` headings
 * (the ten DOC-03 chapters, plus "The master flowchart") — read straight out
 * of the fetched markdown, so it can never list a chapter the guide itself
 * doesn't have. */
export function buildToc(markdown: string): TocEntry[] {
  const entries: TocEntry[] = [];
  const re = /^##\s+(.+)$/gm;
  let match: RegExpExecArray | null;
  while ((match = re.exec(markdown)) !== null) {
    const title = match[1].trim();
    entries.push({ id: slugify(title), title });
  }
  return entries;
}

export function scrollToChapter(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** Tracks the SPA's own light/dark theme (useTheme.ts stamps this attribute
 * on <html>) so the mermaid flowchart re-renders in the matching palette —
 * without this page needing its own theme toggle or prop-drilling one down
 * from App.tsx. */
function useDomTheme(): "light" | "dark" {
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark"),
  );
  useEffect(() => {
    const el = document.documentElement;
    const observer = new MutationObserver(() => {
      setTheme(el.getAttribute("data-theme") === "light" ? "light" : "dark");
    });
    observer.observe(el, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

/** DOC-04: the master flowchart is RENDERED, not shown as mermaid source.
 * Mermaid is lazy-loaded (dynamic import) so its parser/renderer never ships
 * in the main bundle for operators who never open this tab.
 *
 * Honesty note on the innerHTML assignment below: it IS the same primitive
 * as dangerouslySetInnerHTML — no framing makes it otherwise. The actual
 * safety argument is (1) the only source that ever reaches this component
 * is the hash-locked spec file, read server-side from a repo path the
 * client cannot influence, and (2) mermaid runs with securityLevel
 * "strict", its own sanitizing mode. Never point this component at any
 * other markdown source without revisiting both legs. */
export function MermaidDiagram({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const theme = useDomTheme();

  useEffect(() => {
    let cancelled = false;
    setError(null);
    (async () => {
      try {
        const { default: mermaid } = await import("mermaid");
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme === "light" ? "default" : "dark",
        });
        const id = `guide-flowchart-${Math.random().toString(36).slice(2)}`;
        const { svg } = await mermaid.render(id, code);
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg;
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, theme]);

  return (
    <div className="guide-flowchart" data-testid="guide-flowchart">
      {error && (
        <p className="banner-error" role="alert">Flowchart failed to render — {error}</p>
      )}
      <div ref={containerRef} role="img" aria-label="The master flowchart (doc 12)" />
    </div>
  );
}

const mdComponents: Components = {
  // Presentational liberty #1 (see file header): the guide's own top-level
  // heading renders as h2, keeping exactly one h1 on the page.
  h1: ({ children }) => <h2 className="guide-title">{children}</h2>,
  h2: ({ children }) => {
    const id = slugify(flattenText(children));
    return <h2 id={id}>{children}</h2>;
  },
  code: ({ className, children }) => {
    if (className === "language-mermaid") {
      const code = flattenText(children).replace(/\n$/, "");
      // DOC-05 (v1.77, operator-ruled): "the master flowchart and every
      // diagram in the guide render CLICKABLE and ZOOMABLE — click expands
      // to a full-screen view with pan and scroll/pinch zoom, plus explicit
      // zoom controls". This `code`-block handler runs for EVERY fenced
      // ```mermaid block the ratified guide contains (currently just the
      // one master flowchart), so a future second diagram gets the same
      // zoomable treatment for free, not a special case. The child is
      // passed as a THUNK (`() => <MermaidDiagram .../>`), not a plain
      // element, so the inline copy and the overlay copy are independent
      // MermaidDiagram instances with their own ref (see ZoomableFigure.tsx).
      return (
        <ZoomableFigure label="The master flowchart (doc 12)">
          {() => <MermaidDiagram code={code} />}
        </ZoomableFigure>
      );
    }
    return <code className={className}>{children}</code>;
  },
  // The mermaid block above is the guide's ONLY fenced code (everything else
  // is inline `code`), and react-markdown wraps every fenced block in a
  // <pre> — which would leak UA monospace/pre-whitespace styling onto the
  // flowchart wrapper and its error fallback. Unwrap it; the fragment is
  // safe precisely because no other fenced block exists in the ratified
  // guide to lose its formatting.
  pre: ({ children }) => <>{children}</>,
};

export function HowItWorksPage() {
  const [guide, setGuide] = useState<GuideData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getGuide()
      .then((g) => { if (!cancelled) setGuide(g); })
      .catch((e) => { if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, []);

  const toc = useMemo(() => (guide ? buildToc(guide.guide_markdown) : []), [guide]);

  return (
    <div className="how-it-works-page" data-testid="how-it-works-page">
      <h1>How it works</h1>

      {loadError && (
        <p className="banner-error" role="alert">Could not load the guide — {loadError}</p>
      )}

      {!guide && !loadError && (
        <p className="gap-note" data-testid="how-it-works-loading">Loading the guide…</p>
      )}

      {guide && (
        <>
          <p className="guide-stamp" data-testid="guide-version-stamp">
            Describes spec v{guide.guide_version ?? "?"}
          </p>

          {/* DOC-05: banner a stamp/running mismatch instead of pretending
              currency — and banner an UNVERIFIABLE comparison too (either
              version failed to parse server-side). The banner fails toward
              SHOWING: a parse failure silently disabling it would be false
              currency by omission. */}
          {guide.version_mismatch && (
            <div className="banner-error" role="alert" data-testid="guide-mismatch-banner">
              This guide describes spec v{guide.guide_version}, but the running build is on
              spec v{guide.running_spec_version}. Some chapters may not reflect current
              behavior until the guide is re-ratified against the current spec.
            </div>
          )}
          {!guide.version_mismatch && guide.version_unknown && (
            <div className="banner-error" role="alert" data-testid="guide-unknown-banner">
              Cannot verify that this guide matches the running spec:{" "}
              {guide.guide_version === null
                ? "the guide's own version stamp could not be read"
                : "the running build's spec version could not be read"}
              . Treat the chapters as unverified until this is resolved.
            </div>
          )}

          <nav className="guide-toc" aria-label="chapters" data-testid="guide-toc">
            <ul>
              {toc.map((entry) => (
                <li key={entry.id}>
                  <button type="button" onClick={() => scrollToChapter(entry.id)}>
                    {entry.title}
                  </button>
                </li>
              ))}
            </ul>
          </nav>

          <div className="guide-body">
            <ReactMarkdown components={mdComponents}>{guide.guide_markdown}</ReactMarkdown>
          </div>
        </>
      )}
    </div>
  );
}
