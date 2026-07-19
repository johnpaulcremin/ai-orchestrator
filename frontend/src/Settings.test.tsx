import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Settings, type SettingsView } from "./Settings";

function makeView(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    editable: true,
    tiers: [
      {
        key: "OPENAI_MODEL_SMART",
        label: "Smart tier",
        effective_model: "gpt-5",
        source: "default",
        override: null,
        env: null,
        default: "",
        provider: "openai",
        key_env: "OPENAI_API_KEY",
        key_present: true,
      },
    ],
    categories: [
      {
        key: "MODEL_CODING",
        category: "coding",
        label: "Coding",
        tier: "smart",
        effective_model: "gpt-5",
        source: "default",
        override: null,
        env: null,
        inherits: "gpt-5",
        provider: "openai",
        key_env: "OPENAI_API_KEY",
        key_present: true,
      },
    ],
    ...overrides,
  };
}

type Captured = { method: string; url: string; body: unknown };
let requests: Captured[];
let currentView: SettingsView;
let getFailuresRemaining: number;
let cacheEntries: number;
let cacheEnabled: boolean;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ method, url, body });

      if (url.endsWith("/v1/settings") && method === "GET") {
        if (getFailuresRemaining > 0) {
          getFailuresRemaining -= 1;
          return new Response(JSON.stringify({ detail: "Invalid or missing API token" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          });
        }
        return Response.json(currentView);
      }
      if (url.endsWith("/v1/cache") && method === "GET") {
        return Response.json({
          enabled: cacheEnabled,
          entries: cacheEntries,
          ttl_seconds: 0,
          max_entries: 1000,
        });
      }
      if (url.endsWith("/v1/cache") && method === "DELETE") {
        const cleared = cacheEntries;
        cacheEntries = 0;
        return Response.json({ cleared, enabled: cacheEnabled, entries: 0 });
      }
      if (/\/v1\/settings\/[A-Z_]+$/.test(url) && method === "PUT") {
        const coding = { ...currentView.categories[0], source: "override" as const, override: body.value, effective_model: body.value };
        currentView = { ...currentView, categories: [coding] };
        return Response.json(currentView);
      }
      if (/\/v1\/settings\/[A-Z_]+$/.test(url) && method === "DELETE") {
        const coding = { ...currentView.categories[0], source: "default" as const, override: null };
        currentView = { ...currentView, categories: [coding] };
        return Response.json(currentView);
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });

beforeEach(() => {
  requests = [];
  currentView = makeView();
  getFailuresRemaining = 0;
  cacheEntries = 3;
  cacheEnabled = true;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Settings", () => {
  it("loads and renders tier and category rows", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText("Smart tier")).toBeInTheDocument();
    expect(screen.getByText("Coding")).toBeInTheDocument();
    expect(screen.getByText("MODEL_CODING")).toBeInTheDocument();
  });

  it("saves an override via PUT with the entered value", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    const input = await screen.findByLabelText("Coding model");
    await user.type(input, "claude-sonnet-5");
    await user.click(screen.getByRole("button", { name: "Save Coding" }));

    await waitFor(() => {
      const put = requests.find((r) => r.method === "PUT");
      expect(put?.url).toMatch(/\/v1\/settings\/MODEL_CODING$/);
      expect(put?.body).toEqual({ value: "claude-sonnet-5" });
    });
  });

  it("reverts an override via DELETE", async () => {
    currentView = makeView({
      categories: [
        {
          key: "MODEL_CODING",
          category: "coding",
          label: "Coding",
          tier: "smart",
          effective_model: "claude-sonnet-5",
          source: "override",
          override: "claude-sonnet-5",
          env: null,
          inherits: "gpt-5",
          provider: "anthropic",
          key_env: "ANTHROPIC_API_KEY",
          key_present: true,
        },
      ],
    });
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await user.click(await screen.findByRole("button", { name: "Revert Coding" }));

    await waitFor(() => {
      expect(requests.some((r) => r.method === "DELETE")).toBe(true);
    });
  });

  it("disables inputs when editing is not allowed", async () => {
    currentView = makeView({ editable: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByLabelText("Coding model")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save Coding" })).toBeDisabled();
  });

  it("keeps unsaved edits in other rows when saving one row", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    const smart = await screen.findByLabelText("Smart tier model");
    await user.type(smart, "gpt-5");
    await user.type(screen.getByLabelText("Coding model"), "claude-sonnet-5");
    await user.click(screen.getByRole("button", { name: "Save Coding" }));

    // The Smart-tier draft the user typed but did not save must survive.
    await waitFor(() => expect(requests.some((r) => r.method === "PUT")).toBe(true));
    expect(screen.getByLabelText("Smart tier model")).toHaveValue("gpt-5");
  });

  it("closes on Escape even when focus is outside the modal", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={onClose} />);
    await screen.findByText("Smart tier");

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("shows an error with a retry when the initial load fails", async () => {
    getFailuresRemaining = 1; // first GET 401s, retry succeeds
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText(/Failed to load settings \(401\)/)).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).toBeNull();

    await user.click(screen.getByRole("button", { name: /^Retry$/i }));
    expect(await screen.findByText("Smart tier")).toBeInTheDocument();
  });

  it("shows the response-cache size and clears it", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText(/Response cache: 3 stored/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Clear cache/i }));

    expect(await screen.findByText(/Response cache: 0 stored/)).toBeInTheDocument();
    expect(
      requests.some((r) => r.method === "DELETE" && r.url.endsWith("/v1/cache")),
    ).toBe(true);
  });

  it("can still clear residual entries when caching is disabled", async () => {
    cacheEnabled = false;
    cacheEntries = 5;
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText(/5 stored \(caching off\)/)).toBeInTheDocument();
    const clear = screen.getByRole("button", { name: /Clear cache/i });
    expect(clear).toBeEnabled();
    await user.click(clear);
    expect(await screen.findByText(/0 stored/)).toBeInTheDocument();
  });

  it("warns when the required credential is missing", async () => {
    currentView = makeView({
      categories: [
        {
          key: "MODEL_CODING",
          category: "coding",
          label: "Coding",
          tier: "smart",
          effective_model: "gemini/gemini-flash-latest",
          source: "override",
          override: "gemini/gemini-flash-latest",
          env: null,
          inherits: "gpt-5",
          provider: "litellm",
          key_env: "GEMINI_API_KEY",
          key_present: false,
        },
      ],
    });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/GEMINI_API_KEY not set/)).toBeInTheDocument();
  });
});
