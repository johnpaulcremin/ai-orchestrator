import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Share } from "./Share";

let shareState: { active: boolean; token: string | null; expires_at: string | null };
let capturedCreateBody: unknown;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (/\/v1\/conversations\/\d+\/share$/.test(url)) {
        if (method === "GET") {
          return Response.json(shareState);
        }
        if (method === "POST") {
          capturedCreateBody = init?.body ? JSON.parse(String(init.body)) : {};
          shareState = { active: true, token: "abc123token", expires_at: null };
          return Response.json(shareState);
        }
        if (method === "DELETE") {
          shareState = { active: false, token: null, expires_at: null };
          return Response.json(shareState);
        }
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });

beforeEach(() => {
  shareState = { active: false, token: null, expires_at: null };
  capturedCreateBody = null;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Share", () => {
  it("shows a Create share link button when there's no active link", async () => {
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    expect(
      await screen.findByRole("button", { name: /Create share link/i }),
    ).toBeInTheDocument();
  });

  it("shows the existing link immediately when one is already active", async () => {
    shareState = { active: true, token: "existing-token", expires_at: null };
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    expect(await screen.findByLabelText("Share link")).toHaveValue(
      `${window.location.origin}/shared/existing-token`,
    );
    expect(screen.getByText(/Never expires\./i)).toBeInTheDocument();
  });

  it("creates a share link and displays it", async () => {
    const user = userEvent.setup();
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    await user.click(await screen.findByRole("button", { name: /Create share link/i }));

    expect(await screen.findByLabelText("Share link")).toHaveValue(
      `${window.location.origin}/shared/abc123token`,
    );
    expect(capturedCreateBody).toEqual({});
  });

  it("sends the chosen ttl_hours when creating a link", async () => {
    const user = userEvent.setup();
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    await screen.findByRole("button", { name: /Create share link/i });

    await user.selectOptions(screen.getByLabelText("Link expiry"), "24 hours");
    await user.click(screen.getByRole("button", { name: /Create share link/i }));

    expect(await screen.findByLabelText("Share link")).toBeInTheDocument();
    expect(capturedCreateBody).toEqual({ ttl_hours: 24 });
  });

  it("shows the expiry note when the link has an expiry", async () => {
    shareState = { active: true, token: "tok", expires_at: "2026-08-01 00:00:00" };
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    expect(await screen.findByText(/Expires 2026-08-01 00:00:00 UTC\./i)).toBeInTheDocument();
  });

  it("copies the share link to the clipboard", async () => {
    shareState = { active: true, token: "copy-me", expires_at: null };
    const user = userEvent.setup();
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    await screen.findByLabelText("Share link");

    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/shared/copy-me`);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("revokes the link and falls back to the create-link view", async () => {
    shareState = { active: true, token: "revoke-me", expires_at: null };
    const user = userEvent.setup();
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    await screen.findByLabelText("Share link");

    await user.click(screen.getByRole("button", { name: /Revoke link/i }));

    expect(
      await screen.findByRole("button", { name: /Create share link/i }),
    ).toBeInTheDocument();
  });

  it("shows an error message when loading the status fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 500 })),
    );
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={noop} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load share status/i);
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={onClose} />);
    await screen.findByRole("button", { name: /Create share link/i });

    await user.click(screen.getByRole("button", { name: "Close share" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when Done is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Share apiBase="/api" getHeaders={headers} conversationId={10} onClose={onClose} />);
    await screen.findByRole("button", { name: /Create share link/i });

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });
});
