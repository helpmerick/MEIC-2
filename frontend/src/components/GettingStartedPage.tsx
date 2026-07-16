// DOC-06/UI-32 (v1.75 commission, v1.78 ratified content, doc 12 slice 6) —
// the fifth tab: "Getting started". DOC-05 single source, mirroring
// HowItWorksPage: this renders the "# GETTING STARTED" section FROM
// spec/12-how-it-works.md ITSELF, fetched fresh from the backend on every
// mount (GET /getting-started, adapters/api/app.py) — never a frontend copy
// of the prose that could drift out of ratification.
//
// THE tab's one absolute promise (DOC-06): it renders variable NAMES and
// where-to-obtain guidance only — NEVER a current value, password, token, or
// secret. That promise is structural, not editorial: the only content that
// can ever reach this component is the hash-locked spec section's own text,
// read server-side; nothing here (or in the endpoint behind it) reads a live
// .env or any other secret store. Never point this component at any other
// data source.
//
// Two-stamp discipline (v1.78): spec/12 carries TWO independently-stamped
// ratified sections — "# THE GUIDE" (describes v1.72) and "# GETTING
// STARTED" (describes v1.78). This page banners against ITS OWN section's
// stamp only; the sibling guide tab's stamp (and any mismatch it is
// currently, correctly, bannering) never bleeds into this one.
//
// The same two presentational liberties HowItWorksPage takes (flagged in the
// build report there) apply here: the section's own leading "# GETTING
// STARTED (...)" heading renders as an <h2> so the page keeps exactly one
// <h1>, and the section TOC scrolls via scrollIntoView rather than a real
// hash href (router.ts treats window.location.hash as page navigation).
//
// remark-gfm: the ratified section's `.env` annotated template is a GFM
// table (| Variable name | ... |) — react-markdown alone renders GFM tables
// as plain text, so the gfm plugin is required for the template to render as
// the table the operator was promised. The guide has no tables, so
// HowItWorksPage never needed it.
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { GettingStartedData } from "../types";
import {
  MermaidDiagram, buildToc, flattenText, scrollToChapter, slugify,
} from "./HowItWorksPage";
import { ZoomableFigure } from "./ZoomableFigure";

const mdComponents: Components = {
  // Presentational liberty #1 (see file header): the section's own top-level
  // heading renders as h2, keeping exactly one h1 on the page.
  h1: ({ children }) => <h2 className="guide-title">{children}</h2>,
  h2: ({ children }) => {
    const id = slugify(flattenText(children));
    return <h2 id={id}>{children}</h2>;
  },
  code: ({ className, children }) => {
    if (className === "language-mermaid") {
      // DOC-05 (v1.77): every ratified diagram renders clickable and
      // zoomable. The section currently contains no diagrams, but a future
      // re-ratification that adds one gets the guide tab's exact treatment
      // for free rather than a silent raw-source regression.
      const code = flattenText(children).replace(/\n$/, "");
      return (
        <ZoomableFigure label="Getting-started diagram (doc 12)">
          {() => <MermaidDiagram code={code} />}
        </ZoomableFigure>
      );
    }
    return <code className={className}>{children}</code>;
  },
  // Mirror of HowItWorksPage's pre-unwrap: the only fenced block the
  // ratified doc-12 content ever carries is mermaid (handled above), and
  // react-markdown wraps every fenced block in a <pre> that would leak UA
  // monospace/pre-whitespace styling onto the diagram wrapper.
  pre: ({ children }) => <>{children}</>,
};

export function GettingStartedPage() {
  const [data, setData] = useState<GettingStartedData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getGettingStarted()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, []);

  const toc = useMemo(() => (data ? buildToc(data.getting_started_markdown) : []), [data]);

  return (
    <div className="how-it-works-page getting-started-page" data-testid="getting-started-page">
      <h1>Getting started</h1>

      {loadError && (
        <p className="banner-error" role="alert">
          Could not load the getting-started content — {loadError}
        </p>
      )}

      {!data && !loadError && (
        <p className="gap-note" data-testid="getting-started-loading">Loading…</p>
      )}

      {data && (
        <>
          <p className="guide-stamp" data-testid="getting-started-version-stamp">
            Describes spec v{data.getting_started_version ?? "?"}
          </p>

          {/* DOC-05: banner a stamp/running mismatch instead of pretending
              currency — and banner an UNVERIFIABLE comparison too (either
              version failed to parse server-side). Fails toward SHOWING,
              exactly like the guide tab's own banner — but always against
              THIS section's own stamp, never the guide's. */}
          {data.version_mismatch && (
            <div className="banner-error" role="alert" data-testid="getting-started-mismatch-banner">
              This page describes spec v{data.getting_started_version}, but the running build is on
              spec v{data.running_spec_version}. Some steps may not reflect current behavior until
              this section is re-ratified against the current spec.
            </div>
          )}
          {!data.version_mismatch && data.version_unknown && (
            <div className="banner-error" role="alert" data-testid="getting-started-unknown-banner">
              Cannot verify that this page matches the running spec:{" "}
              {data.getting_started_version === null
                ? "this section's own version stamp could not be read"
                : "the running build's spec version could not be read"}
              . Treat these steps as unverified until this is resolved.
            </div>
          )}

          <nav className="guide-toc" aria-label="sections" data-testid="getting-started-toc">
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
            <ReactMarkdown components={mdComponents} remarkPlugins={[remarkGfm]}>
              {data.getting_started_markdown}
            </ReactMarkdown>
          </div>
        </>
      )}
    </div>
  );
}
