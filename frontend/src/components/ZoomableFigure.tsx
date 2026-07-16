// DOC-05 / RPT-12 (v1.77, operator-ruled -- "the text is so small I can't
// see" / the RPT-12 timeline drill-down defect): ONE shared full-screen
// pan/zoom overlay, used by both the how-it-works master flowchart
// (HowItWorksPage.tsx) and the RPT-12 intraday timeline (results/Timeline.tsx)
// -- "apply the shared full-screen pan/zoom component" per the reviewer's
// commission. Click (or Enter/Space) opens a full-screen dialog with
// drag-to-pan, scroll-wheel/pinch zoom, explicit +/- and reset controls, and
// Esc to dismiss (keyboard-accessible throughout).
//
// Hand-rolled CSS `transform: translate() scale()` math -- no pan/zoom
// dependency pulled in. The math is a dozen lines and this component is the
// ONLY place in the app that needs it, so a library would add real bundle
// weight (react-zoom-pan-pinch and friends pull in their own gesture engines)
// for a single call site that SVG/CSS transforms already cover completely.
import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode, TouchEvent, WheelEvent } from "react";

/** The content is a THUNK, not a plain ReactNode -- deliberately. The trigger
 * and the (conditionally mounted) overlay both need their own copy of the
 * content, and a naive `children: ReactNode` prop would hand BOTH locations
 * the exact same element object. React mounts that at two DOM positions
 * fine for a plain, ref-free element (Timeline's chart), but any content
 * that owns a DOM ref (e.g. HowItWorksPage's mermaid-injection div) breaks:
 * a single `useRef` object can only point at one live DOM node, so whichever
 * copy commits last silently steals the ref out from under the other one.
 * Calling `render()` fresh at each site gives each copy its OWN component
 * instance, and therefore its own hooks/refs -- no collision, for any
 * content this ever gets pointed at. */
type Render = () => ReactNode;

const MIN_SCALE = 1;
const MAX_SCALE = 8;
const WHEEL_ZOOM_STEP = 1.25;
const BUTTON_ZOOM_STEP = 1.4;

interface Transform {
  scale: number;
  x: number;
  y: number;
}

const IDENTITY: Transform = { scale: 1, x: 0, y: 0 };

function clampScale(s: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));
}

/** Wraps content (a chart or diagram, passed as `children={() => <X/>}`) so
 * clicking it opens the shared full-screen pan/zoom overlay. Both the inline
 * view and the overlay render from the SAME `render()` call passed in --
 * there is no separate "zoomed" copy of the underlying data or markup to
 * drift out of sync with the inline view, only a fresh component instance
 * per location (see the `Render` type note above for why that matters). */
export function ZoomableFigure({ label, children }: { label: string; children: Render }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLDivElement | null>(null);

  // Dialog a11y: however the overlay is dismissed (Esc, the ✕ button), focus
  // returns to the trigger that opened it -- a keyboard operator is never
  // dropped back at the top of the document after closing.
  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen(true);
    }
  };

  return (
    <div className="zoomable-figure">
      <div
        ref={triggerRef}
        className="zoomable-trigger"
        role="button"
        tabIndex={0}
        aria-label={`${label} — click to enlarge, pan and zoom`}
        data-testid="zoomable-trigger"
        onClick={() => setOpen(true)}
        onKeyDown={onKeyDown}
      >
        {children()}
        <span className="zoomable-hint" aria-hidden="true">
          ⤢ click to enlarge
        </span>
      </div>
      {open && (
        <ZoomOverlay label={label} onClose={close}>
          {children()}
        </ZoomOverlay>
      )}
    </div>
  );
}

function ZoomOverlay({
  label,
  onClose,
  children,
}: {
  label: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const [t, setT] = useState<Transform>(IDENTITY);
  const dragRef = useRef<{ startClientX: number; startClientY: number; startX: number; startY: number } | null>(
    null,
  );
  const pinchDistRef = useRef<number | null>(null);
  const closeBtnRef = useRef<HTMLButtonElement | null>(null);

  // Esc-dismissable (keyboard accessible) + body-scroll lock while the
  // full-screen overlay is open, restored on close/unmount either way.
  useEffect(() => {
    closeBtnRef.current?.focus();
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  const zoomBy = useCallback((factor: number) => {
    setT((prev) => ({ ...prev, scale: clampScale(prev.scale * factor) }));
  }, []);

  const reset = useCallback(() => setT(IDENTITY), []);

  const onWheel = (e: WheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? WHEEL_ZOOM_STEP : 1 / WHEEL_ZOOM_STEP);
  };

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    dragRef.current = { startClientX: e.clientX, startClientY: e.clientY, startX: t.x, startY: t.y };
  };
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = e.clientX - drag.startClientX;
    const dy = e.clientY - drag.startClientY;
    setT((prev) => ({ ...prev, x: drag.startX + dx, y: drag.startY + dy }));
  };
  const endDrag = () => {
    dragRef.current = null;
  };

  // Pinch-to-zoom: Pointer Events don't expose "distance between two active
  // pointers" directly, so the two-finger gesture is tracked via native
  // touch events instead (parallel to, not instead of, the pointer-based pan
  // above -- a single-finger touch still drives onPointerMove/onPointerDown).
  const onTouchMove = (e: TouchEvent<HTMLDivElement>) => {
    if (e.touches.length !== 2) return;
    e.preventDefault();
    const [a, b] = [e.touches[0], e.touches[1]];
    const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    if (pinchDistRef.current != null && pinchDistRef.current > 0) {
      zoomBy(dist / pinchDistRef.current);
    }
    pinchDistRef.current = dist;
  };
  const onTouchEnd = (e: TouchEvent<HTMLDivElement>) => {
    if (e.touches.length < 2) pinchDistRef.current = null;
  };

  return (
    <div
      className="zoom-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={`${label} — full-screen zoomable view`}
      data-testid="zoom-overlay"
    >
      <div className="zoom-toolbar">
        <span className="zoom-toolbar-label">{label}</span>
        <button type="button" onClick={() => zoomBy(BUTTON_ZOOM_STEP)} aria-label="Zoom in">
          +
        </button>
        <button type="button" onClick={() => zoomBy(1 / BUTTON_ZOOM_STEP)} aria-label="Zoom out">
          −
        </button>
        <button type="button" onClick={reset} aria-label="Reset zoom and pan">
          Reset
        </button>
        <button
          type="button"
          ref={closeBtnRef}
          onClick={onClose}
          aria-label="Close full-screen view"
          className="zoom-close"
        >
          ✕
        </button>
      </div>
      <div
        className="zoom-viewport"
        data-testid="zoom-viewport"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        <div
          className="zoom-content"
          data-testid="zoom-content"
          style={{ transform: `translate(${t.x}px, ${t.y}px) scale(${t.scale})` }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
