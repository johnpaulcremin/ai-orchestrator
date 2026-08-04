import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ErrorBoundary } from "./ErrorBoundary";
import { reportClientError } from "./crashReporter";

// componentDidCatch forwards to the crash reporter (which POSTs) — mock it so
// these tests don't fire a real fetch, and so the forwarding can be asserted.
vi.mock("./crashReporter", () => ({ reportClientError: vi.fn() }));

function Boom(): never {
  throw new Error("kaboom");
}

describe("ErrorBoundary", () => {
  beforeEach(() => {
    vi.mocked(reportClientError).mockClear();
  });

  it("renders children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>all good</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("all good")).toBeInTheDocument();
  });

  it("catches a render-time error and shows a recoverable message", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      );
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText("kaboom")).toBeInTheDocument();
      expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    } finally {
      consoleError.mockRestore();
    }
  });

  it("includes the label in the fallback heading when provided", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ErrorBoundary label="Usage">
          <Boom />
        </ErrorBoundary>,
      );
      expect(screen.getByText("Usage: something went wrong")).toBeInTheDocument();
    } finally {
      consoleError.mockRestore();
    }
  });

  it("forwards a caught error to the crash reporter, with the label and component stack", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ErrorBoundary label="Usage">
          <Boom />
        </ErrorBoundary>,
      );
      expect(reportClientError).toHaveBeenCalledTimes(1);
      const [message, stack] = vi.mocked(reportClientError).mock.calls[0];
      expect(message).toBe("[Usage] kaboom");
      expect(stack).toContain("Component stack:");
    } finally {
      consoleError.mockRestore();
    }
  });

  it("shows the error stack and component stack behind a details disclosure", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    try {
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      );
      const details = screen.getByText("Show details").closest("details");
      expect(details).not.toHaveAttribute("open");

      await user.click(screen.getByText("Show details"));
      expect(screen.getAllByText(/kaboom/).length).toBeGreaterThan(0);
      expect(screen.getByText(/Component stack/)).toBeInTheDocument();
    } finally {
      consoleError.mockRestore();
    }
  });

  it("reloads the page when Reload is clicked", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const user = userEvent.setup();
    const reload = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, reload },
    });
    try {
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      );
      await user.click(screen.getByRole("button", { name: "Reload" }));
      expect(reload).toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: originalLocation,
      });
      consoleError.mockRestore();
    }
  });
});
