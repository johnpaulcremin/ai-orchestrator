import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PRESETS } from "./setupPresets";
import { SetupWizard } from "./SetupWizard";

type Recorded = { method: string; url: string; body?: unknown };
let requests: Recorded[];
let testKeyStatus: number;
let testKeyBody: Record<string, unknown>;
let settingsPutStatus: number;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ method, url, body });

      if (url.endsWith("/v1/setup/test-key") && method === "POST") {
        if (testKeyStatus !== 200) {
          return new Response(JSON.stringify({ detail: "nope" }), { status: testKeyStatus });
        }
        return Response.json(testKeyBody);
      }
      if (url.includes("/v1/settings/") && method === "PUT") {
        if (settingsPutStatus !== 200) {
          return new Response(JSON.stringify({ detail: "read-only" }), {
            status: settingsPutStatus,
          });
        }
        return Response.json({ tiers: [], categories: [], features: [] });
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

function renderWizard(overrides: Partial<Parameters<typeof SetupWizard>[0]> = {}) {
  const onClose = vi.fn();
  const onChanged = vi.fn();
  render(
    <SetupWizard
      apiBase="/api"
      getHeaders={(extra) => ({ ...(extra ?? {}) })}
      onClose={onClose}
      onChanged={onChanged}
      credentialsConfigured={false}
      {...overrides}
    />,
  );
  return { onClose, onChanged };
}

beforeEach(() => {
  requests = [];
  testKeyStatus = 200;
  testKeyBody = {
    ok: true,
    outcome: "ok",
    model: "gpt-5-nano",
    key_env: "OPENAI_API_KEY",
    detail: "The key works.",
  };
  settingsPutStatus = 200;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SetupWizard", () => {
  it("titles itself with an h2 and names its three steps with h3s", () => {
    // app/codebase_inventory.py derives the app's own account of its panels
    // from these headings; a wizard nobody can find in the self-description
    // is half a feature.
    renderWizard();
    expect(screen.getByRole("dialog", { name: "First-run setup" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "First-run setup" })).toBeInTheDocument();
    for (const step of ["Add your API key", "Choose a model preset", "Restart and finish"]) {
      expect(screen.getByRole("heading", { level: 3, name: step })).toBeInTheDocument();
    }
  });

  it("tests the pasted key against the backend and shows the env line on success", async () => {
    const user = userEvent.setup();
    renderWizard();

    const input = screen.getByLabelText("OpenAI API key");
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, "sk-abcdefghijklmnop");
    await user.click(screen.getByRole("button", { name: "Test API key" }));

    expect(await screen.findByText(/The key works\./)).toBeInTheDocument();
    const call = requests.find((r) => r.url.endsWith("/v1/setup/test-key"));
    expect(call?.body).toEqual({ api_key: "sk-abcdefghijklmnop" });

    // The env line is shown MASKED — the full secret is only ever copied.
    const line = screen.getByLabelText("Line to add to .env");
    expect(line.textContent).toMatch(/^OPENAI_API_KEY=sk-a•+mnop$/);
    expect(line.textContent).not.toContain("sk-abcdefghijklmnop");
  });

  it("reports a rejected key without showing the env line", async () => {
    const user = userEvent.setup();
    testKeyBody = {
      ok: false,
      outcome: "auth_failed",
      model: "gpt-5-nano",
      key_env: "OPENAI_API_KEY",
      detail: "The provider rejected this key.",
    };
    renderWizard();
    await user.type(screen.getByLabelText("OpenAI API key"), "sk-wrong");
    await user.click(screen.getByRole("button", { name: "Test API key" }));

    expect(await screen.findByText(/rejected this key/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Line to add to .env")).not.toBeInTheDocument();
  });

  it("explains a 401 as needing sign-in rather than as a bad key", async () => {
    const user = userEvent.setup();
    testKeyStatus = 401;
    renderWizard();
    await user.type(screen.getByLabelText("OpenAI API key"), "sk-x");
    await user.click(screen.getByRole("button", { name: "Test API key" }));
    expect(await screen.findByText(/Sign in first/)).toBeInTheDocument();
  });

  it("disables Test until something is typed", () => {
    renderWizard();
    expect(screen.getByRole("button", { name: "Test API key" })).toBeDisabled();
  });

  it("applies a preset as one PUT per tier key and reports it takes effect at once", async () => {
    const user = userEvent.setup();
    const { onChanged } = renderWizard();
    await user.click(screen.getByLabelText("Cheapest", { exact: false }));
    await user.click(screen.getByRole("button", { name: "Apply model preset" }));

    expect(await screen.findByText(/no restart needed/)).toBeInTheDocument();
    const puts = requests.filter((r) => r.method === "PUT");
    const cheapest = PRESETS.find((p) => p.id === "cheapest")!;
    expect(puts).toHaveLength(Object.keys(cheapest.values).length);
    for (const [key, value] of Object.entries(cheapest.values)) {
      expect(puts.find((r) => r.url.endsWith(`/v1/settings/${key}`))?.body).toEqual({ value });
    }
    expect(onChanged).toHaveBeenCalled();
  });

  it("degrades to a clear read-only message when settings are locked", async () => {
    const user = userEvent.setup();
    settingsPutStatus = 403;
    const { onChanged } = renderWizard();
    await user.click(screen.getByRole("button", { name: "Apply model preset" }));
    expect(await screen.findByText(/read-only on this deployment/)).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("only offers model names this repo already uses", () => {
    // A preset must never invent an id the operator then has to debug.
    const allowed = new Set(["gpt-5", "gpt-5-mini", "gpt-5-nano"]);
    for (const preset of PRESETS) {
      for (const value of Object.values(preset.values)) {
        expect(allowed.has(value)).toBe(true);
      }
    }
  });

  it("changes its copy when the key is already configured", () => {
    renderWizard({ credentialsConfigured: true });
    expect(screen.getByText(/API key is configured/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing to restart/)).toBeInTheDocument();
  });

  it("closes on Done and on the close button", async () => {
    const user = userEvent.setup();
    const { onClose } = renderWizard();
    await user.click(screen.getByRole("button", { name: "Finish setup" }));
    await user.click(screen.getByRole("button", { name: "Close setup" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
