// Small reusable disclosure bubble (v1.63 UI-18a Presentation ruling): shows
// extra detail without ever using a native `title` attribute — `title` is
// invisible to touch and cannot be reached by keyboard focus, so a touch or
// keyboard-only operator can never read it. This shows on mouseenter,
// keyboard focus, AND tap/click of the trigger; hides on mouseleave, blur,
// Escape, and an outside tap. The bubble is `role="tooltip"`, positioned
// `position:fixed` from the trigger's own bounding rect so it escapes any
// `overflow-x:auto` ancestor (the same technique CalendarHeatmap's hover box
// already uses for `.sched-scroll`-like containers).
import { useEffect, useRef, useState } from "react";

export function Tooltip({
  id,
  content,
  testId,
  label,
}: {
  /** id of the bubble — pass the SAME id as the disclosing input/control's
   * `aria-describedby` so a screen-reader operator on that control reaches
   * this content too. */
  id: string;
  content: string;
  testId: string;
  /** Accessible name for the trigger button (there is no visible label). */
  label: string;
}) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  const place = () => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos({ x: r.left + r.width / 2, y: r.top });
  };
  const show = () => {
    place();
    setOpen(true);
  };
  const hide = () => setOpen(false);

  // Outside tap dismisses it — a real pointerdown elsewhere on the page, not
  // the click that opened it (that click never reaches this listener: the
  // listener is only attached once `open` is already true, on the NEXT
  // render after this same click has already finished dispatching).
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (triggerRef.current && !triggerRef.current.contains(e.target as Node)) hide();
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  return (
    <span className="tooltip-wrap">
      <button
        ref={triggerRef}
        type="button"
        className="tooltip-trigger"
        aria-label={label}
        aria-describedby={id}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={(e) => {
          e.stopPropagation();
          if (open) hide();
          else show();
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") hide();
        }}
      >
        <span aria-hidden="true">ⓘ</span>
      </button>
      {/* Always-present, visually-hidden copy carrying `id`, so the trigger's
          (and the disclosing input's) `aria-describedby` ALWAYS resolves. The
          visible bubble below only exists while open — pointing a describedby
          at it left the reference dangling for a keyboard operator tabbed onto
          the control (final-review finding, 2026-07-12). This node holds no
          role/testid so the open/closed `role="tooltip"` assertions are
          unaffected. */}
      <span id={id} className="sr-only">{content}</span>
      {open && (
        <span
          role="tooltip"
          className="app-tooltip"
          data-testid={testId}
          style={pos ? { left: pos.x, top: pos.y } : undefined}
        >
          {content}
        </span>
      )}
    </span>
  );
}
