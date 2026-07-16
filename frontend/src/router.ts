import { useEffect, useState } from "react";

// UI-27 (operator rule, 2026-07-10): the Results dashboard is a SEPARATE
// CLIENT-SIDE ROUTE inside this one SPA — not a new app, not server routing.
// A tiny hash router is enough: no library is installed, hash changes never
// hit the server, and a hash URL is deep-linkable (bookmarkable, shareable)
// without any backend route changes.
// CAL-08/UI-30 (v1.71): "Calendar" is a separate client-side route, same
// pattern as Results (UI-27). DOC-05/UI-29: "How it works" is ALSO a route —
// v1.72 ratified doc 12's guide content, so HowItWorksPage now renders it
// (fetched from GET /guide, DOC-05 single source) rather than the honest
// placeholder this page showed before ratification.
// DOC-06/UI-32 (v1.75 commission, v1.78 ratified content): "Getting started"
// is the FIFTH tab — same DOC-05 single-source pattern over spec/12's own
// "# GETTING STARTED" section (GET /getting-started).
export type Route =
  | { page: "trading" }
  | { page: "results" }
  | { page: "results-day"; date: string }
  | { page: "calendar" }
  | { page: "how-it-works" }
  | { page: "getting-started" };

const DAY_RE = /^\/results\/day\/(\d{4}-\d{2}-\d{2})$/;

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "");
  const dayMatch = DAY_RE.exec(path);
  if (dayMatch) return { page: "results-day", date: dayMatch[1] };
  if (path === "/results") return { page: "results" };
  if (path === "/calendar") return { page: "calendar" };
  if (path === "/how-it-works") return { page: "how-it-works" };
  if (path === "/getting-started") return { page: "getting-started" };
  return { page: "trading" }; // default hash ("" or "/") — Trading keeps its today
}

/** Reactive current route, updated on hashchange (back/forward + nav clicks). */
export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function resultsDayHref(isoDate: string): string {
  return `#/results/day/${isoDate}`;
}
