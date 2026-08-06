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

// jsdom does not implement matchMedia; Composer.tsx's mobile-placeholder
// breakpoint hook calls it on every render. jsdom's default viewport is wide
// (1024px), so every query resolves as non-matching ("desktop") here --
// correct for every existing test, none of which exercise the narrow-width
// placeholder swap.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

// jsdom does not implement ResizeObserver; Composer.tsx's --composer-height
// reporting (App.css's .jump-to-bottom reads it) calls it on mount. Fires
// the callback once, synchronously, on observe() -- jsdom never computes
// real layout (offsetHeight is always 0), so there's no later resize to
// simulate, but this is enough for a test to confirm the effect actually
// ran and wired up the CSS variable rather than silently doing nothing.
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    #callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) {
      this.#callback = callback;
    }
    observe(target: Element) {
      this.#callback([{ target } as ResizeObserverEntry], this as unknown as ResizeObserver);
    }
    unobserve() {}
    disconnect() {}
  };
}
