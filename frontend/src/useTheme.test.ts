import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useTheme } from "./useTheme";

function stubMatchMedia(matchesLight: boolean) {
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: matchesLight, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }));
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("useTheme", () => {
  it("defaults to dark, toggles, persists, and stamps data-theme on <html>", () => {
    stubMatchMedia(false); // OS is not light
    const { result } = renderHook(() => useTheme());

    expect(result.current[0]).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    act(() => result.current[1]()); // toggle

    expect(result.current[0]).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(localStorage.getItem("meic_theme")).toBe("light");
  });

  it("honours an explicit saved theme over the OS preference", () => {
    stubMatchMedia(false);
    localStorage.setItem("meic_theme", "light");
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("light");
  });

  it("follows the OS preference when nothing is saved", () => {
    stubMatchMedia(true); // OS prefers light
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("light");
  });
});
