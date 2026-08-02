import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Users, type AdminUser } from "./Users";

function makeUser(overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id: 1,
    username: "grandma",
    created_at: "2026-08-01 10:00:00",
    is_active: true,
    must_change_password: false,
    last_login_at: null,
    ...overrides,
  };
}

type Captured = { method: string; url: string; body: unknown };
let requests: Captured[];
let currentUsers: AdminUser[];
let createResponse: { user: AdminUser; temporary_password: string } | { detail: string };
let createStatus: number;
let resetPasswordValue: string;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ method, url, body });

      if (url.endsWith("/v1/users") && method === "GET") {
        return Response.json(currentUsers);
      }
      if (url.endsWith("/v1/users") && method === "POST") {
        return new Response(JSON.stringify(createResponse), {
          status: createStatus,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/reset-password") && method === "POST") {
        return Response.json({ temporary_password: resetPasswordValue });
      }
      if (url.endsWith("/deactivate") && method === "POST") {
        const username = url.split("/").slice(-2)[0];
        currentUsers = currentUsers.map((u) =>
          u.username === username ? { ...u, is_active: false } : u,
        );
        return Response.json(currentUsers.find((u) => u.username === username));
      }
      if (url.endsWith("/reactivate") && method === "POST") {
        const username = url.split("/").slice(-2)[0];
        currentUsers = currentUsers.map((u) =>
          u.username === username ? { ...u, is_active: true } : u,
        );
        return Response.json(currentUsers.find((u) => u.username === username));
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const headers = (extra: Record<string, string> = {}) => ({ ...extra });

describe("Users", () => {
  beforeEach(() => {
    requests = [];
    currentUsers = [makeUser()];
    createResponse = {
      user: makeUser({ username: "newkid", must_change_password: true }),
      temporary_password: "temp-pw-123",
    };
    createStatus = 201;
    resetPasswordValue = "reset-pw-456";
    stubFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists existing users", async () => {
    render(<Users apiBase="/api" getHeaders={headers} />);
    expect(await screen.findByText("grandma")).toBeInTheDocument();
  });

  it("shows an empty state when there are no users", async () => {
    currentUsers = [];
    render(<Users apiBase="/api" getHeaders={headers} />);
    expect(await screen.findByText("No users yet.")).toBeInTheDocument();
  });

  it("creates a user and reveals the temporary password once", async () => {
    const user = userEvent.setup();
    render(<Users apiBase="/api" getHeaders={headers} />);
    await screen.findByText("grandma");

    await user.type(screen.getByLabelText("New username"), "newkid");
    await user.click(screen.getByRole("button", { name: "Add user" }));

    expect(await screen.findByText("temp-pw-123")).toBeInTheDocument();
    expect(screen.getByText(/write this down now/i)).toBeInTheDocument();

    // Dismissing the reveal clears it.
    await user.click(screen.getByRole("button", { name: "I've saved it" }));
    expect(screen.queryByText("temp-pw-123")).not.toBeInTheDocument();
  });

  it("shows an error when creating a duplicate username", async () => {
    createStatus = 409;
    createResponse = { detail: "Username already exists." };
    const user = userEvent.setup();
    render(<Users apiBase="/api" getHeaders={headers} />);
    await screen.findByText("grandma");

    await user.type(screen.getByLabelText("New username"), "grandma");
    await user.click(screen.getByRole("button", { name: "Add user" }));

    expect(await screen.findByText("Username already exists.")).toBeInTheDocument();
  });

  it("copies the revealed temporary password to the clipboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<Users apiBase="/api" getHeaders={headers} />);
    await screen.findByText("grandma");

    await user.type(screen.getByLabelText("New username"), "newkid");
    await user.click(screen.getByRole("button", { name: "Add user" }));
    await screen.findByText("temp-pw-123");

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("temp-pw-123");
    expect(await screen.findByRole("button", { name: "Copied!" })).toBeInTheDocument();
  });

  it("resets a user's password and reveals the new temporary password", async () => {
    const user = userEvent.setup();
    render(<Users apiBase="/api" getHeaders={headers} />);
    const row = (await screen.findByText("grandma")).closest("tr") as HTMLElement;

    await user.click(within(row).getByRole("button", { name: "Reset password" }));

    expect(await screen.findByText("reset-pw-456")).toBeInTheDocument();
    const resetRequest = requests.find((r) => r.url.endsWith("/users/grandma/reset-password"));
    expect(resetRequest?.method).toBe("POST");
  });

  it("deactivates and reactivates a user", async () => {
    const user = userEvent.setup();
    render(<Users apiBase="/api" getHeaders={headers} />);
    let row = (await screen.findByText("grandma")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Active")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Deactivate" }));

    await waitFor(() => {
      row = screen.getByText("grandma").closest("tr") as HTMLElement;
      expect(within(row).getByText("Deactivated")).toBeInTheDocument();
    });

    row = screen.getByText("grandma").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Reactivate" }));

    await waitFor(() => {
      row = screen.getByText("grandma").closest("tr") as HTMLElement;
      expect(within(row).getByText("Active")).toBeInTheDocument();
    });
  });

  it("flags accounts that must change their password", async () => {
    currentUsers = [makeUser({ must_change_password: true })];
    render(<Users apiBase="/api" getHeaders={headers} />);
    expect(await screen.findByText(/must change password/)).toBeInTheDocument();
  });
});
