import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Unmount React trees between tests so component tests stay isolated.
afterEach(() => {
  cleanup();
});

// jsdom does not implement scrollIntoView; the auto-scroll effect calls it.
if (!window.HTMLElement.prototype.scrollIntoView) {
  window.HTMLElement.prototype.scrollIntoView = () => {};
}
