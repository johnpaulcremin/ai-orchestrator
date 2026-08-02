import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChangePassword } from "./ChangePassword";

type Captured = { method: string; url: string; body: unknown };
let requests: Captured[];
let shouldFail: string | null;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ method, url, body });

      if (url.endsWith("/v1/auth/change-password") && method === "POST") {
        if (shouldFail) {
          return new Response(JSON.stringify({ detail: shouldFail }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json({ username: "grandma", is_admin: false, must_change_password: false });
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const headers = (extra: Record<string, string> = {}) => ({ ...extra });

describe("ChangePassword", () => {
  beforeEach(() => {
    requests = [];
    shouldFail = null;
    stubFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("submits the current and new password, then calls onChanged", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    render(
      <ChangePassword
        apiBase="/api"
        getHeaders={headers}
        username="grandma"
        onChanged={onChanged}
        onSignOut={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText(/temporary \/ current password/i), "temp-pw");
    await user.type(screen.getByLabelText(/^new password$/i), "newpassword123");
    await user.type(screen.getByLabelText(/confirm new password/i), "newpassword123");
    await user.click(screen.getByRole("button", { name: "Save password" }));

    expect(onChanged).toHaveBeenCalled();
    const req = requests.find((r) => r.url.endsWith("/v1/auth/change-password"));
    expect(req?.body).toEqual({
      current_password: "temp-pw",
      new_password: "newpassword123",
    });
  });

  it("rejects a new password shorter than 8 characters without calling the API", async () => {
    const user = userEvent.setup();
    render(
      <ChangePassword
        apiBase="/api"
        getHeaders={headers}
        username="grandma"
        onChanged={vi.fn()}
        onSignOut={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText(/temporary \/ current password/i), "temp-pw");
    await user.type(screen.getByLabelText(/^new password$/i), "short");
    await user.type(screen.getByLabelText(/confirm new password/i), "short");
    await user.click(screen.getByRole("button", { name: "Save password" }));

    expect(await screen.findByText(/at least 8 characters/i)).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });

  it("rejects a mismatched confirmation without calling the API", async () => {
    const user = userEvent.setup();
    render(
      <ChangePassword
        apiBase="/api"
        getHeaders={headers}
        username="grandma"
        onChanged={vi.fn()}
        onSignOut={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText(/temporary \/ current password/i), "temp-pw");
    await user.type(screen.getByLabelText(/^new password$/i), "newpassword123");
    await user.type(screen.getByLabelText(/confirm new password/i), "different123");
    await user.click(screen.getByRole("button", { name: "Save password" }));

    expect(await screen.findByText(/don't match/i)).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });

  it("shows a server error and does not call onChanged on a wrong current password", async () => {
    shouldFail = "Current password is incorrect.";
    const user = userEvent.setup();
    const onChanged = vi.fn();
    render(
      <ChangePassword
        apiBase="/api"
        getHeaders={headers}
        username="grandma"
        onChanged={onChanged}
        onSignOut={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText(/temporary \/ current password/i), "wrong");
    await user.type(screen.getByLabelText(/^new password$/i), "newpassword123");
    await user.type(screen.getByLabelText(/confirm new password/i), "newpassword123");
    await user.click(screen.getByRole("button", { name: "Save password" }));

    expect(await screen.findByText("Current password is incorrect.")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("calls onSignOut when 'Sign out instead' is clicked", async () => {
    const user = userEvent.setup();
    const onSignOut = vi.fn();
    render(
      <ChangePassword
        apiBase="/api"
        getHeaders={headers}
        username="grandma"
        onChanged={vi.fn()}
        onSignOut={onSignOut}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Sign out instead" }));
    expect(onSignOut).toHaveBeenCalled();
  });
});
