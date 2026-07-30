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
    features: [
      {
        key: "CODE_EXECUTION",
        label: "Code execution",
        description: "Lets the model run Python to verify a calculation or snippet.",
        effective_enabled: false,
        source: "default",
        override: null,
        env: null,
        default: false,
      },
    ],
    prompts: [
      {
        key: "CATEGORY_PROMPT_SUMMARIZATION",
        category: "summarization",
        label: "Summarization",
        effective_prompt: "",
        source: "default",
        override: null,
        env: null,
        default: "",
      },
    ],
    free_lane: [
      {
        key: "FREE_TIER_MODELS",
        label: "Free-tier models (ordered, comma-separated)",
        effective_value: "",
        source: "default",
        override: null,
        env: null,
        default: "",
      },
      {
        key: "FREE_TIER_DEFAULT_QUOTA",
        label: "Default daily quota per model",
        effective_value: "100",
        source: "default",
        override: null,
        env: null,
        default: "100",
      },
    ],
    ...overrides,
  };
}

type ModelCatalogStatus = {
  enabled: boolean;
  synced_at: string | null;
  model_count: number;
  new_models: string[];
  stale: boolean;
  error?: string | null;
};

type Captured = { method: string; url: string; body: unknown };
let requests: Captured[];
let currentView: SettingsView;
let getFailuresRemaining: number;
let cacheEntries: number;
let cacheEnabled: boolean;
let putShouldFailForKey: string | null;
let modelCatalogStatus: ModelCatalogStatus;
let modelCatalogAfterSync: ModelCatalogStatus | null;

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
      if (url.endsWith("/v1/model-catalog") && method === "GET") {
        return Response.json(modelCatalogStatus);
      }
      if (url.endsWith("/v1/model-catalog/sync") && method === "POST") {
        if (modelCatalogAfterSync) {
          modelCatalogStatus = modelCatalogAfterSync;
        }
        return Response.json(modelCatalogStatus);
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
        const applyFeatureOverride = (item: SettingsView["features"][number]) =>
          item.key === key
            ? {
                ...item,
                source: "override" as const,
                override: body.value,
                effective_enabled: body.value === "true",
              }
            : item;
        const applyPromptOverride = (item: SettingsView["prompts"][number]) =>
          item.key === key
            ? {
                ...item,
                source: "override" as const,
                override: body.value,
                effective_prompt: body.value,
              }
            : item;
        const applyFreeLaneOverride = (item: SettingsView["free_lane"][number]) =>
          item.key === key
            ? { ...item, source: "override" as const, override: body.value, effective_value: body.value }
            : item;
        currentView = {
          ...currentView,
          tiers: currentView.tiers.map(applyOverride),
          categories: currentView.categories.map(applyOverride),
          features: currentView.features.map(applyFeatureOverride),
          prompts: currentView.prompts.map(applyPromptOverride),
          free_lane: currentView.free_lane.map(applyFreeLaneOverride),
        };
        return Response.json(currentView);
      }
      if (/\/v1\/settings\/[A-Z_]+$/.test(url) && method === "DELETE") {
        const key = url.split("/").pop();
        const clearOverride = (item: SettingsView["tiers"][number]) =>
          item.key === key ? { ...item, source: "default" as const, override: null } : item;
        const clearFeatureOverride = (item: SettingsView["features"][number]) =>
          item.key === key ? { ...item, source: "default" as const, override: null } : item;
        const clearPromptOverride = (item: SettingsView["prompts"][number]) =>
          item.key === key
            ? { ...item, source: "default" as const, override: null, effective_prompt: "" }
            : item;
        const clearFreeLaneOverride = (item: SettingsView["free_lane"][number]) =>
          item.key === key
            ? { ...item, source: "default" as const, override: null, effective_value: item.default }
            : item;
        currentView = {
          ...currentView,
          tiers: currentView.tiers.map(clearOverride),
          categories: currentView.categories.map(clearOverride),
          features: currentView.features.map(clearFeatureOverride),
          prompts: currentView.prompts.map(clearPromptOverride),
          free_lane: currentView.free_lane.map(clearFreeLaneOverride),
        };
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
  modelCatalogStatus = {
    enabled: false,
    synced_at: null,
    model_count: 0,
    new_models: [],
    stale: false,
  };
  modelCatalogAfterSync = null;
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

  it("filters tiers, categories, and features by label or key as you type", async () => {
    currentView = makeView({
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
        {
          key: "MODEL_MATH",
          category: "math",
          label: "Math",
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
    });
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");
    expect(screen.getByText("Coding")).toBeInTheDocument();
    expect(screen.getByText("Math")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search settings"), "coding");

    expect(screen.getByText("Coding")).toBeInTheDocument();
    expect(screen.queryByText("Math")).not.toBeInTheDocument();
    expect(screen.queryByText("Smart tier")).not.toBeInTheDocument();
    expect(screen.queryByText("Optional features")).not.toBeInTheDocument();
  });

  it("matches on the settings key too, not just the label", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Coding");

    await user.type(screen.getByLabelText("Search settings"), "MODEL_CODING");

    expect(screen.getByText("Coding")).toBeInTheDocument();
    expect(screen.queryByText("Smart tier")).not.toBeInTheDocument();
  });

  it("shows a no-matches message when the settings search matches nothing", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Coding");

    await user.type(screen.getByLabelText("Search settings"), "nonexistent term xyz");

    expect(await screen.findByText(/No settings match "nonexistent term xyz"/i)).toBeInTheDocument();
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

  it("renders the Optional features section with a checkbox per flag", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText("Optional features")).toBeInTheDocument();
    const checkbox = screen.getByLabelText("Code execution");
    expect(checkbox).toBeInstanceOf(HTMLInputElement);
    expect((checkbox as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole("button", { name: "Revert Code execution" })).toBeDisabled();
  });

  it("toggles a feature flag via PUT and reflects the new state", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    const checkbox = await screen.findByLabelText("Code execution");
    await user.click(checkbox);

    await waitFor(() => {
      const put = requests.find((r) => r.method === "PUT");
      expect(put?.url).toMatch(/\/v1\/settings\/CODE_EXECUTION$/);
      expect(put?.body).toEqual({ value: "true" });
    });
    expect(checkbox).toBeChecked();
  });

  it("disables the Revert button until a feature flag has an override, then clears it via DELETE", async () => {
    currentView = makeView({
      features: [
        {
          key: "CODE_EXECUTION",
          label: "Code execution",
          description: "Lets the model run Python to verify a calculation or snippet.",
          effective_enabled: true,
          source: "override",
          override: "true",
          env: null,
          default: false,
        },
      ],
    });
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    const revertButton = await screen.findByRole("button", { name: "Revert Code execution" });
    expect(revertButton).not.toBeDisabled();

    await user.click(revertButton);

    await waitFor(() => {
      const del = requests.find((r) => r.method === "DELETE");
      expect(del?.url).toMatch(/\/v1\/settings\/CODE_EXECUTION$/);
    });
  });

  it("disables feature-flag checkboxes and Revert when editing is not allowed", async () => {
    currentView = makeView({ editable: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByLabelText("Code execution")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Revert Code execution" })).toBeDisabled();
  });

  it("renders the Role prompts section with a textarea per category", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText("Role prompts")).toBeInTheDocument();
    const textarea = screen.getByLabelText("Summarization role prompt");
    expect(textarea).toBeInstanceOf(HTMLTextAreaElement);
    expect((textarea as HTMLTextAreaElement).value).toBe("");
    expect(screen.getByRole("button", { name: "Revert Summarization role prompt" })).toBeDisabled();
  });

  it("saves a role prompt via PUT with the entered value", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    const textarea = await screen.findByLabelText("Summarization role prompt");
    await user.type(textarea, "You are a senior engineer.");
    await user.click(screen.getByRole("button", { name: "Save Summarization role prompt" }));

    await waitFor(() => {
      const put = requests.find((r) => r.method === "PUT");
      expect(put?.url).toMatch(/\/v1\/settings\/CATEGORY_PROMPT_SUMMARIZATION$/);
      expect(put?.body).toEqual({ value: "You are a senior engineer." });
    });
  });

  it("reverts a role prompt override via DELETE", async () => {
    currentView = makeView({
      prompts: [
        {
          key: "CATEGORY_PROMPT_SUMMARIZATION",
          category: "summarization",
          label: "Summarization",
          effective_prompt: "You are a senior engineer.",
          source: "override",
          override: "You are a senior engineer.",
          env: null,
          default: "",
        },
      ],
    });
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    const revertButton = await screen.findByRole("button", {
      name: "Revert Summarization role prompt",
    });
    expect(revertButton).not.toBeDisabled();
    await user.click(revertButton);

    await waitFor(() => {
      const del = requests.find((r) => r.method === "DELETE");
      expect(del?.url).toMatch(/\/v1\/settings\/CATEGORY_PROMPT_SUMMARIZATION$/);
    });
  });

  it("disables the role-prompt textarea and Revert when editing is not allowed", async () => {
    currentView = makeView({ editable: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByLabelText("Summarization role prompt")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Revert Summarization role prompt" }),
    ).toBeDisabled();
  });

  it("matches role prompts in the settings search by label or key", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Role prompts");

    await user.type(screen.getByLabelText("Search settings"), "CATEGORY_PROMPT");
    expect(screen.getByText("Role prompts")).toBeInTheDocument();
    expect(screen.queryByText("Optional features")).not.toBeInTheDocument();
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

  it("shows the model catalog as off when sync is disabled", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/Model catalog: sync off/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sync now/i })).toBeDisabled();
  });

  it("shows 'not synced yet' when enabled but no sync has completed", async () => {
    modelCatalogStatus = {
      enabled: true,
      synced_at: null,
      model_count: 0,
      new_models: [],
      stale: true,
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/Model catalog: not synced yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sync now/i })).toBeEnabled();
  });

  it("shows the synced model count and timestamp", async () => {
    modelCatalogStatus = {
      enabled: true,
      synced_at: "2026-07-29 10:00:00",
      model_count: 1234,
      new_models: [],
      stale: false,
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(
      await screen.findByText(/Model catalog: 1,234 models synced 2026-07-29 10:00:00/),
    ).toBeInTheDocument();
  });

  it("shows a new-models notice after a sync surfaces one", async () => {
    modelCatalogStatus = {
      enabled: true,
      synced_at: "2026-07-29 10:00:00",
      model_count: 10,
      new_models: ["gpt-6", "claude-opus-5"],
      stale: false,
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(
      await screen.findByText(/🆕 2 new models since the last sync: gpt-6, claude-opus-5/),
    ).toBeInTheDocument();
  });

  it("clicking Sync now triggers a manual sync and updates the status", async () => {
    modelCatalogStatus = {
      enabled: true,
      synced_at: null,
      model_count: 0,
      new_models: [],
      stale: true,
    };
    modelCatalogAfterSync = {
      enabled: true,
      synced_at: "2026-07-29 12:00:00",
      model_count: 42,
      new_models: [],
      stale: false,
    };
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText(/Model catalog: not synced yet/);

    await user.click(screen.getByRole("button", { name: /Sync now/i }));

    expect(
      await screen.findByText(/Model catalog: 42 models synced 2026-07-29 12:00:00/),
    ).toBeInTheDocument();
    expect(
      requests.some(
        (r) => r.method === "POST" && r.url.endsWith("/v1/model-catalog/sync"),
      ),
    ).toBe(true);
  });

  it("shows a sync error without discarding the last known status", async () => {
    modelCatalogStatus = {
      enabled: true,
      synced_at: "2026-07-29 09:00:00",
      model_count: 5,
      new_models: [],
      stale: false,
      error: "Sync failed — see server logs. Last known catalog kept.",
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(
      await screen.findByText(/Sync failed — see server logs\. Last known catalog kept\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Model catalog: 5 models synced/)).toBeInTheDocument();
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

  it("renders the free-first routing lane section", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText("Free-first routing lane")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Free-tier models (ordered, comma-separated)"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Default daily quota per model")).toBeInTheDocument();
  });

  it("saves the free-tier model list via PUT", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    const input = await screen.findByLabelText("Free-tier models (ordered, comma-separated)");
    await user.type(input, "groq/llama-3.3-70b-versatile");
    await user.click(
      screen.getByRole("button", { name: "Save Free-tier models (ordered, comma-separated)" }),
    );

    await waitFor(() => {
      const put = requests.find((r) => r.method === "PUT");
      expect(put?.url).toMatch(/\/v1\/settings\/FREE_TIER_MODELS$/);
      expect(put?.body).toEqual({ value: "groq/llama-3.3-70b-versatile" });
    });
  });

  it("reverts the free-tier default quota override via DELETE", async () => {
    currentView = makeView({
      free_lane: [
        {
          key: "FREE_TIER_MODELS",
          label: "Free-tier models (ordered, comma-separated)",
          effective_value: "",
          source: "default",
          override: null,
          env: null,
          default: "",
        },
        {
          key: "FREE_TIER_DEFAULT_QUOTA",
          label: "Default daily quota per model",
          effective_value: "50",
          source: "override",
          override: "50",
          env: null,
          default: "100",
        },
      ],
    });
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await user.click(
      await screen.findByRole("button", { name: "Revert Default daily quota per model" }),
    );

    await waitFor(() => {
      const del = requests.find((r) => r.method === "DELETE");
      expect(del?.url).toMatch(/\/v1\/settings\/FREE_TIER_DEFAULT_QUOTA$/);
    });
  });

  it("matches the free-lane section in settings search", async () => {
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Free-first routing lane");

    await user.type(screen.getByLabelText("Search settings"), "FREE_TIER_MODELS");
    expect(screen.getByText("Free-first routing lane")).toBeInTheDocument();
    expect(screen.queryByText("Optional features")).not.toBeInTheDocument();
  });

  // --- malformed/partial backend responses: must degrade, never crash ---------

  it("does not crash when the settings response is missing the free_lane key", async () => {
    const rest: Record<string, unknown> = { ...makeView() };
    delete rest.free_lane;
    currentView = rest as unknown as ReturnType<typeof makeView>;
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("Smart tier")).toBeInTheDocument();
    expect(screen.queryByText("Free-first routing lane")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert", { name: /something went wrong/i })).not.toBeInTheDocument();
  });

  it("does not crash when the settings response is missing tiers/categories/features/prompts", async () => {
    currentView = {
      editable: true,
      tiers: undefined,
      categories: undefined,
      features: undefined,
      prompts: undefined,
      free_lane: undefined,
    } as unknown as ReturnType<typeof makeView>;
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    // Nothing to show, but it must render the (empty) panel, not crash.
    await screen.findByLabelText("Search settings");
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  it("does not crash when the model catalog response is missing new_models", async () => {
    modelCatalogStatus = {
      enabled: true,
      synced_at: "2026-07-29 10:00:00",
      model_count: 10,
      stale: false,
    } as unknown as {
      enabled: boolean;
      synced_at: string | null;
      model_count: number;
      new_models: string[];
      stale: boolean;
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText(/Model catalog: 10 models synced 2026-07-29 10:00:00/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/new model/)).not.toBeInTheDocument();
  });
});
