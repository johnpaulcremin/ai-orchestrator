import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Summarize } from "./Summarize";

let summaryShouldFail: boolean;
let capturedUrl: string | null;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (/\/v1\/conversations\/\d+\/summarize$/.test(url) && method === "POST") {
        capturedUrl = url;
        if (summaryShouldFail) {
          return new Response(JSON.stringify({ detail: "Summarization failed." }), {
            status: 502,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json({ summary: "Discussed ramen spots; decided on Ichiran." });
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });

beforeEach(() => {
  summaryShouldFail = false;
  capturedUrl = null;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Summarize", () => {
  it("requests and renders the summary for the given conversation", async () => {
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />,
    );
    expect(
      await screen.findByText("Discussed ramen spots; decided on Ichiran."),
    ).toBeInTheDocument();
    expect(capturedUrl).toBe("/api/v1/conversations/10/summarize");
  });

  it("shows a loading state before the summary arrives", async () => {
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />,
    );
    expect(screen.getByText(/Summarizing…/i)).toBeInTheDocument();
    await screen.findByText("Discussed ramen spots; decided on Ichiran.");
  });

  it("shows the server's error detail when summarization fails", async () => {
    summaryShouldFail = true;
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/Summarization failed\./i);
  });

  it("copies the summary to the clipboard", async () => {
    const user = userEvent.setup();
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />,
    );
    await screen.findByText("Discussed ramen spots; decided on Ichiran.");

    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    await user.click(screen.getByRole("button", { name: /📋 Copy/i }));

    expect(writeText).toHaveBeenCalledWith("Discussed ramen spots; decided on Ichiran.");
    expect(await screen.findByRole("button", { name: /✓ Copied/i })).toBeInTheDocument();
  });

  it("does not show a Copy button while there is no summary yet", async () => {
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />,
    );
    expect(screen.queryByRole("button", { name: /📋 Copy/i })).not.toBeInTheDocument();
    await screen.findByText("Discussed ramen spots; decided on Ichiran.");
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={onClose} />,
    );
    await screen.findByText("Discussed ramen spots; decided on Ichiran.");

    await user.click(screen.getByRole("button", { name: "Close summary" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when Done is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Summarize apiBase="/api" getHeaders={headers} conversationId={10} onClose={onClose} />,
    );
    await screen.findByText("Discussed ramen spots; decided on Ichiran.");

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });
});
