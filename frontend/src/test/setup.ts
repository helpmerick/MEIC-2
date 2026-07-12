import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// unmount + clear the DOM between tests (RTL auto-cleanup only registers when
// afterEach is global; we register it explicitly instead).
afterEach(() => cleanup());
