import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SharedConversation } from "./SharedConversation";

function setPath(path: string) {
  window.history.pushState({}, "", path);
}

function stubFetch(handler: (url: string) => Response | Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      return handler(url);
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("SharedConversation", () => {
  it("shows an error when the URL has no share token", async () => {
    setPath("/");
    render(<SharedConversation />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /No share link found in this URL\./i,
    );
  });

  it("fetches by the token in the URL and renders the title and messages", async () => {
    setPath("/shared/my-token-123");
    let requestedUrl = "";
    stubFetch((url) => {
      requestedUrl = url;
      return Response.json({
        title: "Ramen spots",
        created_at: "2026-07-18 10:00:00",
        messages: [
          { role: "user", content: "Best ramen in town?", created_at: "2026-07-18 10:00:00" },
          { role: "assistant", content: "Try **Ichiran**.", created_at: "2026-07-18 10:01:00" },
        ],
      });
    });

    render(<SharedConversation />);

    expect(await screen.findByRole("heading", { name: "Ramen spots" })).toBeInTheDocument();
    expect(screen.getByText("Best ramen in town?")).toBeInTheDocument();
    expect(screen.getByText("Ichiran")).toBeInTheDocument(); // rendered from markdown bold
    expect(requestedUrl).toBe("/api/v1/shared/my-token-123");
  });

  it("shows a friendly message for an invalid or expired link (404)", async () => {
    setPath("/shared/expired-token");
    stubFetch(() => new Response(null, { status: 404 }));

    render(<SharedConversation />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /This share link is invalid or has expired\./i,
    );
  });

  it("shows a generic error for a non-404 failure", async () => {
    setPath("/shared/some-token");
    stubFetch(() => new Response(null, { status: 500 }));

    render(<SharedConversation />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Failed to load this conversation \(500\)\./i,
    );
  });

  it("renders sources, fact-checks, and math results on shared messages", async () => {
    setPath("/shared/rich-token");
    stubFetch(() =>
      Response.json({
        title: "Rich message",
        created_at: "2026-07-18 10:00:00",
        messages: [
          {
            role: "assistant",
            content: "See below.",
            created_at: "2026-07-18 10:00:00",
            sources: [{ title: "Example", url: "https://example.com" }],
            fact_checks: [
              { claim: "The moon landing was faked", rating: "False", publisher: "Snopes", url: "https://snopes.com" },
            ],
            academic_results: [
              {
                title: "Climate Adaptation Strategies",
                authors: "A. Researcher",
                year: 2022,
                venue: "Nature",
                url: "https://example.com/paper",
              },
            ],
            math_results: [
              { operation: "solve", expression: "x**2 - 4", variable: "x", result: "[-2, 2]" },
            ],
          },
        ],
      }),
    );

    render(<SharedConversation />);

    expect(await screen.findByRole("link", { name: "Example" })).toHaveAttribute(
      "href",
      "https://example.com",
    );
    expect(screen.getByText("False")).toBeInTheDocument();
    expect(screen.getByText("The moon landing was faked")).toBeInTheDocument();
    expect(screen.getByText("Climate Adaptation Strategies")).toBeInTheDocument();
    expect(screen.getByText("A. Researcher · 2022")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Nature" })).toHaveAttribute(
      "href",
      "https://example.com/paper",
    );
    expect(screen.getByText("x**2 - 4")).toBeInTheDocument();
    expect(screen.getByText("= [-2, 2]")).toBeInTheDocument();
  });

  it("shows a read-only banner making clear this isn't a live chat", async () => {
    setPath("/shared/token-x");
    stubFetch(() =>
      Response.json({ title: "t", created_at: "2026-07-18 10:00:00", messages: [] }),
    );
    render(<SharedConversation />);
    expect(
      await screen.findByText(/Read-only shared conversation/i),
    ).toBeInTheDocument();
  });
});
