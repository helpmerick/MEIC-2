// DOC-05 / RPT-12 (v1.77) -- the shared full-screen pan/zoom overlay. Pinned
// here once, reused by HowItWorksPage's flowchart and results/Timeline's
// chart (see each component's own test for "opens the shared overlay").
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef } from "react";
import { describe, expect, it } from "vitest";

import { ZoomableFigure } from "./ZoomableFigure";

function renderFigure() {
  return render(
    <ZoomableFigure label="Test figure">
      {() => <div data-testid="figure-content">content</div>}
    </ZoomableFigure>,
  );
}

describe("ZoomableFigure (shared pan/zoom overlay)", () => {
  it("renders the content inline, with no overlay open yet", () => {
    renderFigure();
    expect(screen.getByTestId("figure-content")).toBeInTheDocument();
    expect(screen.queryByTestId("zoom-overlay")).not.toBeInTheDocument();
  });

  it("clicking the figure opens a full-screen dialog", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const overlay = await screen.findByTestId("zoom-overlay");
    expect(overlay).toBeInTheDocument();
    expect(overlay).toHaveAttribute("role", "dialog");
    expect(overlay).toHaveAttribute("aria-modal", "true");
  });

  it("is keyboard-operable: Enter on the trigger opens it too", async () => {
    renderFigure();
    const trigger = screen.getByTestId("zoomable-trigger");
    trigger.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByTestId("zoom-overlay")).toBeInTheDocument();
  });

  it("Esc dismisses the full-screen view (keyboard-dismissable)", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    await screen.findByTestId("zoom-overlay");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("zoom-overlay")).not.toBeInTheDocument();
  });

  it("the close button dismisses the view", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    await userEvent.click(screen.getByRole("button", { name: /close full-screen view/i }));
    expect(screen.queryByTestId("zoom-overlay")).not.toBeInTheDocument();
  });

  it("returns focus to the trigger on dismiss (dialog a11y), via Esc AND via the close button", async () => {
    renderFigure();
    const trigger = screen.getByTestId("zoomable-trigger");

    // Esc path.
    trigger.focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByTestId("zoom-overlay");
    // The overlay moved focus (to its close button) on open -- so the
    // assertion below is genuinely the restore, not focus never leaving.
    expect(trigger).not.toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(trigger).toHaveFocus();

    // Close-button path.
    await userEvent.keyboard("{Enter}");
    await screen.findByTestId("zoom-overlay");
    await userEvent.click(screen.getByRole("button", { name: /close full-screen view/i }));
    expect(trigger).toHaveFocus();
  });

  it("the same content renders again inside the overlay (single source, not a second copy)", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const overlay = await screen.findByTestId("zoom-overlay");
    expect(overlay.querySelector('[data-testid="figure-content"]')).toBeInTheDocument();
  });

  it("a ref-owning child gets its own independent instance inline and in the overlay (no ref collision)", async () => {
    // Regression: `children` is a THUNK, called fresh at each render site,
    // specifically so a child that owns a DOM ref (like HowItWorksPage's
    // mermaid-injection div) never has its single ref object stolen by
    // whichever of the two mounted copies commits last.
    function RefOwner() {
      const ref = useRef<HTMLDivElement | null>(null);
      useEffect(() => {
        if (ref.current) ref.current.textContent = "mine";
      }, []);
      return <div ref={ref} data-testid="ref-owner" />;
    }
    render(
      <ZoomableFigure label="Ref test">{() => <RefOwner />}</ZoomableFigure>,
    );
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const owners = await screen.findAllByTestId("ref-owner");
    expect(owners).toHaveLength(2);
    // Both independently ran their own effect and stamped their own node --
    // neither is left blank because a shared ref pointed only at the other.
    for (const owner of owners) expect(owner).toHaveTextContent("mine");
  });

  it("explicit +/- controls change the zoom scale", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const content = await screen.findByTestId("zoom-content");
    expect(content.style.transform).toContain("scale(1)");

    await userEvent.click(screen.getByRole("button", { name: /zoom in/i }));
    expect(content.style.transform).toContain("scale(1.4)");

    await userEvent.click(screen.getByRole("button", { name: /zoom out/i }));
    expect(content.style.transform).toContain("scale(1)");
  });

  it("reset returns scale and pan to identity", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    await userEvent.click(screen.getByRole("button", { name: /zoom in/i }));
    await userEvent.click(screen.getByRole("button", { name: /reset/i }));
    const content = screen.getByTestId("zoom-content");
    expect(content.style.transform).toBe("translate(0px, 0px) scale(1)");
  });

  it("scrolling the viewport (wheel) zooms in and out", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const viewport = await screen.findByTestId("zoom-viewport");
    const content = screen.getByTestId("zoom-content");

    fireEvent.wheel(viewport, { deltaY: -100 });
    expect(content.style.transform).toContain("scale(1.25)");

    fireEvent.wheel(viewport, { deltaY: 100 });
    expect(content.style.transform).toContain("scale(1)");
  });

  it("dragging the viewport (pointer down/move) pans the content", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const viewport = await screen.findByTestId("zoom-viewport");
    const content = screen.getByTestId("zoom-content");

    fireEvent.pointerDown(viewport, { clientX: 100, clientY: 100 });
    fireEvent.pointerMove(viewport, { clientX: 140, clientY: 130 });
    expect(content.style.transform).toContain("translate(40px, 30px)");

    fireEvent.pointerUp(viewport);
    fireEvent.pointerMove(viewport, { clientX: 999, clientY: 999 });
    // No drag active after pointer-up -- further moves must not keep panning.
    expect(content.style.transform).toContain("translate(40px, 30px)");
  });

  it("zoom never exceeds the max scale (8x) or goes below the min (1x)", async () => {
    renderFigure();
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    const zoomIn = screen.getByRole("button", { name: /zoom in/i });
    const zoomOut = screen.getByRole("button", { name: /zoom out/i });
    const content = screen.getByTestId("zoom-content");

    for (let i = 0; i < 20; i++) await userEvent.click(zoomIn);
    expect(content.style.transform).toContain("scale(8)");

    for (let i = 0; i < 30; i++) await userEvent.click(zoomOut);
    expect(content.style.transform).toContain("scale(1)");
  });
});
