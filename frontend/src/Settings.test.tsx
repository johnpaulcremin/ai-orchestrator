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
let putShouldFailForKey: string | null;

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
        const key = url.split("/").pop();
        if (key === putShouldFailForKey) {
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          });
        }
        const applyOverride = (item: SettingsView["tiers"][number]) =>
          item.key === key
            ? { ...item, source: "override" as const, override: body.value, effective_model: body.value }
            : item;
        currentView = {
          ...currentView,
          tiers: currentView.tiers.map(applyOverride),
          categories: currentView.categories.map(applyOverride),
        };
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
  putShouldFailForKey = null;
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

  it("moves keyboard focus into the dialog when it opens", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");
    expect(screen.getByRole("dialog")).toContainElement(document.activeElement as HTMLElement);
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

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load settings \(401\)/);
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

  it("exports the current overrides as a JSON config file", async () => {
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
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    let capturedBlob: Blob | null = null;
    let capturedFilename = "";
    URL.createObjectURL = vi.fn((blob: Blob) => {
      capturedBlob = blob;
      return "blob:fake-url";
    });
    URL.revokeObjectURL = vi.fn();
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        capturedFilename = this.download;
      });

    try {
      const user = userEvent.setup();
      render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
      await screen.findByText("Smart tier");

      await user.click(screen.getByRole("button", { name: "⬇️ Export config" }));

      expect(capturedBlob).not.toBeNull();
      expect(capturedBlob?.type).toBe("application/json");
      expect(capturedFilename).toBe("ai-workbench-settings.json");
      const text = await capturedBlob?.text();
      expect(JSON.parse(text ?? "{}")).toEqual({ overrides: { MODEL_CODING: "claude-sonnet-5" } });
      expect(await screen.findByText("Exported 1 override.")).toBeInTheDocument();
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("notes an empty export when no overrides are set", async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:fake-url");
    URL.revokeObjectURL = vi.fn();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    try {
      const user = userEvent.setup();
      render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
      await screen.findByText("Smart tier");

      await user.click(screen.getByRole("button", { name: "⬇️ Export config" }));

      expect(
        await screen.findByText("No overrides are set — exported an empty file."),
      ).toBeInTheDocument();
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("imports overrides from a JSON config file", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");

    const config = JSON.stringify({
      overrides: { MODEL_CODING: "claude-sonnet-5", OPENAI_MODEL_SMART: "gpt-5" },
    });
    const file = new File([config], "ai-workbench-settings.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import settings config from a JSON file/i);
    await user.upload(input, file);

    expect(await screen.findByText("Imported 2 overrides.")).toBeInTheDocument();
    const puts = requests.filter((r) => r.method === "PUT");
    expect(puts.map((r) => r.url).sort()).toEqual([
      "/api/v1/settings/MODEL_CODING",
      "/api/v1/settings/OPENAI_MODEL_SMART",
    ]);
  });

  it("reports a partial failure when importing several overrides", async () => {
    putShouldFailForKey = "OPENAI_MODEL_SMART";
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");

    const config = JSON.stringify({
      overrides: { MODEL_CODING: "claude-sonnet-5", OPENAI_MODEL_SMART: "gpt-5" },
    });
    const file = new File([config], "settings.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import settings config from a JSON file/i);
    await user.upload(input, file);

    expect(
      await screen.findByText("Imported 1 of 2 overrides (1 failed)."),
    ).toBeInTheDocument();
  });

  it("shows an error for a config file with no overrides", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");

    const file = new File(["{}"], "empty.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import settings config from a JSON file/i);
    await user.upload(input, file);

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent(/doesn't contain any settings overrides/i);
  });

  it("shows an error for invalid JSON on config import", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");

    const file = new File(["not json"], "settings.json", { type: "application/json" });
    const input = screen.getByLabelText(/Import settings config from a JSON file/i);
    await user.upload(input, file);

    expect(await screen.findByRole("alert")).toHaveTextContent(/valid JSON/i);
  });

  it("disables Import config but not Export config when editing is not allowed", async () => {
    currentView = makeView({ editable: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");

    expect(screen.getByRole("button", { name: "⬆️ Import config" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "⬇️ Export config" })).toBeEnabled();
  });
});
