import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Settings, type SettingsView } from "./Settings";

function makeView(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    editable: true,
    admin_gated: false,
    is_admin: false,
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
        credential: null,
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
    retention: [
      {
        key: "RETENTION_DAYS_DETAIL",
        label: "Detail retention (days)",
        effective_value: "365",
        source: "default",
        override: null,
        env: null,
        default: "365",
      },
      {
        key: "SHARE_EXPIRY_DAYS",
        label: "Default share-link expiry (days, blank = never)",
        effective_value: "",
        source: "default",
        override: null,
        env: null,
        default: "",
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
let settingsGetShould401: boolean;
let cacheEntries: number;
let cacheEnabled: boolean;
let putShouldFailForKey: string | null;
let modelCatalogStatus: ModelCatalogStatus;
let modelCatalogAfterSync: ModelCatalogStatus | null;
let memoryEntries: number;
let memoryEnabled: boolean;
let semanticCacheEntries: number;
let semanticCacheEnabled: boolean;
let correctionSummaryResponse: unknown;
let fallbackSummaryResponse: unknown;
let retryCostSummaryResponse: unknown;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      requests.push({ method, url, body });

      if (url.endsWith("/v1/settings") && method === "GET") {
        if (settingsGetShould401) {
          return new Response(JSON.stringify({ detail: "Invalid or missing API token" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (getFailuresRemaining > 0) {
          getFailuresRemaining -= 1;
          return new Response(JSON.stringify({ detail: "boom" }), {
            status: 500,
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
      if (url.endsWith("/v1/users") && method === "GET") {
        // Only reachable when the current view's is_admin is true (the Users
        // section is otherwise never rendered, so never fetched).
        return Response.json([]);
      }
      if (url.endsWith("/v1/cache") && method === "DELETE") {
        const cleared = cacheEntries;
        cacheEntries = 0;
        return Response.json({ cleared, enabled: cacheEnabled, entries: 0 });
      }
      if (url.endsWith("/v1/memory") && method === "GET") {
        return Response.json({
          enabled: memoryEnabled,
          entries: memoryEntries,
          threshold: 0.75,
          top_k: 5,
          max_entries: 500,
        });
      }
      if (url.endsWith("/v1/memory") && method === "DELETE") {
        const cleared = memoryEntries;
        memoryEntries = 0;
        return Response.json({
          cleared,
          enabled: memoryEnabled,
          entries: 0,
          threshold: 0.75,
          top_k: 5,
          max_entries: 500,
        });
      }
      if (url.endsWith("/v1/semantic-cache") && method === "GET") {
        return Response.json({
          enabled: semanticCacheEnabled,
          entries: semanticCacheEntries,
          threshold: 0.9,
          max_entries: 1000,
        });
      }
      if (url.endsWith("/v1/semantic-cache") && method === "DELETE") {
        const cleared = semanticCacheEntries;
        semanticCacheEntries = 0;
        return Response.json({
          cleared,
          enabled: semanticCacheEnabled,
          entries: 0,
          threshold: 0.9,
          max_entries: 1000,
        });
      }
      if (url.endsWith("/v1/correction/summary") && method === "GET") {
        return Response.json(correctionSummaryResponse);
      }
      if (url.endsWith("/v1/fallback/summary") && method === "GET") {
        return Response.json(fallbackSummaryResponse);
      }
      if (url.endsWith("/v1/retry-cost/summary") && method === "GET") {
        return Response.json(retryCostSummaryResponse);
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
          retention: currentView.retention.map(applyFreeLaneOverride),
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
          retention: currentView.retention.map(clearFreeLaneOverride),
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
  settingsGetShould401 = false;
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
  memoryEntries = 4;
  memoryEnabled = true;
  semanticCacheEntries = 2;
  semanticCacheEnabled = true;
  correctionSummaryResponse = {
    overall: { flagged: 1, answers: 4, correction_rate: 0.25 },
    by_model: {},
    by_category: {},
    by_lane: {},
  };
  fallbackSummaryResponse = { reasons: [] };
  retryCostSummaryResponse = {
    overall: {
      turns: 5,
      retried_turns: 2,
      retries: 2,
      corrections: 0,
      unpriced_attempts: 0,
      first_attempt_cost_usd: 0.05,
      total_cost_usd: 0.14,
      retry_cost_usd: 0.09,
      cost_multiplier: 2.8,
      retry_rate: 0.4,
      retry_rate_ci: [0.1181, 0.7695],
      turns_for_directional: 92,
      reads_as: "insufficient",
    },
    by_signal: {
      regenerated_unrated: { retries: 1, retry_cost_usd: 0.04 },
      regenerated_after_downvote: { retries: 1, retry_cost_usd: 0.05 },
      regenerated_after_upvote: { retries: 0, retry_cost_usd: 0 },
      edited: { retries: 0, retry_cost_usd: 0 },
    },
  };
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
          credential: null,
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
    getFailuresRemaining = 1; // first GET 500s, retry succeeds
    const user = userEvent.setup();
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} jwtEnabled={false} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load settings \(500\)/);
    expect(screen.queryByText("Loading…")).toBeNull();

    await user.click(screen.getByRole("button", { name: /^Retry$/i }));
    expect(await screen.findByText("Smart tier")).toBeInTheDocument();
  });

  // --- 401 wording matches the auth mode actually in use -----------------

  it("shows a session-expired message on a 401 when this deployment uses JWT accounts", async () => {
    settingsGetShould401 = true;
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} jwtEnabled={true} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your session has expired — please sign in again.",
    );
  });

  it("shows a rejected-token message on a 401 when this deployment uses a static API token", async () => {
    settingsGetShould401 = true;
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} jwtEnabled={false} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your API token was rejected — enter a valid one in the sidebar.",
    );
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

  // --- Admin gate: Users section visibility + read-only banner ---------------

  it("hides the Users section for a non-admin caller", async () => {
    currentView = makeView({ is_admin: false, admin_gated: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Smart tier");
    expect(screen.queryByRole("heading", { name: "Users" })).not.toBeInTheDocument();
  });

  it("shows the Users section for an admin caller", async () => {
    currentView = makeView({ is_admin: true, admin_gated: true });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByRole("heading", { name: "Users" })).toBeInTheDocument();
  });

  it("shows the admin-gated read-only banner for a locked-out non-admin", async () => {
    currentView = makeView({ editable: false, admin_gated: true, is_admin: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText(/Settings are managed by an admin account/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/ALLOW_SETTINGS_WRITE=false/)).not.toBeInTheDocument();
  });

  it("shows the ALLOW_SETTINGS_WRITE banner when not admin-gated", async () => {
    currentView = makeView({ editable: false, admin_gated: false, is_admin: false });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText(/ALLOW_SETTINGS_WRITE=false/)).toBeInTheDocument();
    expect(
      screen.queryByText(/Settings are managed by an admin account/i),
    ).not.toBeInTheDocument();
  });

  // --- Cross-conversation memory section --------------------------------------

  it("shows cross-conversation memory stats and clears it", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText(/Cross-conversation memory: 4 stored/),
    ).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Clear memory" }));

    expect(
      await screen.findByText(/Cross-conversation memory: 0 stored/),
    ).toBeInTheDocument();
    expect(
      requests.some((r) => r.method === "DELETE" && r.url.endsWith("/v1/memory")),
    ).toBe(true);
  });

  it("hides the memory section when the endpoint is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.endsWith("/v1/memory")) {
          throw new Error("network error");
        }
        if (url.endsWith("/v1/settings") && (init?.method ?? "GET") === "GET") {
          return Response.json(currentView);
        }
        return new Response(null, { status: 404 });
      }),
    );
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Smart tier");
    expect(screen.queryByText(/Cross-conversation memory/)).not.toBeInTheDocument();
  });

  // --- Semantic cache section ---------------------------------------------------

  it("shows semantic cache stats and clears it", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText(/Semantic cache: 2 stored/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Clear semantic cache" }));

    expect(await screen.findByText(/Semantic cache: 0 stored/)).toBeInTheDocument();
    expect(
      requests.some(
        (r) => r.method === "DELETE" && r.url.endsWith("/v1/semantic-cache"),
      ),
    ).toBe(true);
  });

  // --- Data retention section ---------------------------------------------------

  it("shows and saves the data retention settings", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(await screen.findByText("Data retention")).toBeInTheDocument();
    expect(screen.getByText("Detail retention (days)")).toBeInTheDocument();
    expect(
      screen.getByText("Default share-link expiry (days, blank = never)"),
    ).toBeInTheDocument();

    const user = userEvent.setup();
    const input = screen.getByLabelText("Detail retention (days)");
    await user.clear(input);
    await user.type(input, "30");
    await user.click(screen.getByRole("button", { name: "Save Detail retention (days)" }));

    await waitFor(() => {
      expect(
        requests.some(
          (r) =>
            r.method === "PUT" &&
            r.url.endsWith("/v1/settings/RETENTION_DAYS_DETAIL") &&
            (r.body as { value: string }).value === "30",
        ),
      ).toBe(true);
    });
  });

  it("matches the data retention section in settings search", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("Smart tier");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Search settings"), "share-link");

    expect(
      screen.getByText("Default share-link expiry (days, blank = never)"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Smart tier")).not.toBeInTheDocument();
  });

  // --- Implicit-correction / paid-fallback-cause stats ---------------------------

  it("shows the implicit correction rate and paid fallback causes on demand", async () => {
    fallbackSummaryResponse = {
      reasons: [
        { reason: "timeout", count: 2 },
        { reason: "budget_refusal", count: 1 },
      ],
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    expect(
      await screen.findByText(/Implicit correction rate: 25% \(1\/4\)/),
    ).toBeInTheDocument();
    expect(screen.getByText(/noisy proxy/)).toBeInTheDocument();
    expect(
      screen.getByText(/Paid fallback causes: timeout \(2\), budget refusal \(1\)/),
    ).toBeInTheDocument();
  });

  it("shows re-run cost with its n, its interval, and what it cannot support", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    // Never the bare percentage: n, the 95% interval, and the sufficiency
    // verdict travel with it (see app/retry_cost.py).
    const line = await screen.findByText(/Re-run cost: true cost/);
    expect(line).toHaveTextContent("true cost $0.14 vs $0.05 first-attempt (2.80×)");
    expect(line).toHaveTextContent("retry rate 40% (2/5 turns)");
    expect(line).toHaveTextContent("95% CI 12%–77%");
    expect(line).toHaveTextContent("too few to be a finding (~92 turns at this rate would be)");
  });

  it("splits the re-run reasons instead of showing one retry count", async () => {
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    const line = await screen.findByText(/Why re-run:/);
    expect(line).toHaveTextContent("regenerated, unrated (may be taste) (1)");
    expect(line).toHaveTextContent("regenerated after 👎 (quality failure) (1)");
    // A signal with no re-runs is not listed as a zero-noise entry.
    expect(line).not.toHaveTextContent("edited and re-asked");
  });

  it("hides the re-run cost line when there are no turns in the window", async () => {
    retryCostSummaryResponse = {
      overall: {
        turns: 0,
        retried_turns: 0,
        retries: 0,
        corrections: 0,
        unpriced_attempts: 0,
        first_attempt_cost_usd: 0,
        total_cost_usd: 0,
        retry_cost_usd: 0,
        cost_multiplier: null,
        retry_rate: 0,
        retry_rate_ci: null,
        turns_for_directional: null,
        reads_as: "no_data",
      },
      by_signal: {},
    };
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Smart tier");
    expect(screen.queryByText(/Re-run cost:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Why re-run:/)).not.toBeInTheDocument();
  });

  it("hides the quality-signals block when the endpoints are unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === "string" ? input : input.toString();
        if (
          url.endsWith("/v1/correction/summary") ||
          url.endsWith("/v1/fallback/summary") ||
          url.endsWith("/v1/retry-cost/summary")
        ) {
          throw new Error("network error");
        }
        if (url.endsWith("/v1/settings") && (init?.method ?? "GET") === "GET") {
          return Response.json(currentView);
        }
        return new Response(null, { status: 404 });
      }),
    );
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Smart tier");
    expect(screen.queryByText(/Implicit correction rate/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Paid fallback causes/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Re-run cost:/)).not.toBeInTheDocument();
  });
});

describe("Settings feature-flag credentials", () => {
  const flag = (
    overrides: Partial<SettingsView["features"][number]>,
  ): SettingsView["features"][number] => ({
    key: "FACT_CHECK",
    label: "Fact-check lookup",
    description: "Looks up published fact-checks.",
    effective_enabled: false,
    source: "default",
    override: null,
    env: null,
    default: false,
    credential: null,
    ...overrides,
  });

  it("warns when a required key is missing on an enabled feature", async () => {
    // The state that is broken RIGHT NOW: FACT_CHECK with no key returns
    // before making any request, so it is indistinguishable from working.
    currentView = makeView({
      features: [
        flag({
          effective_enabled: true,
          credential: { key_env: "GOOGLE_FACT_CHECK_API_KEY", key_present: false, required: true },
        }),
      ],
    });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    const warning = await screen.findByText(/⚠ GOOGLE_FACT_CHECK_API_KEY not set/);
    expect(warning).toHaveClass("key-warning");
  });

  it("only advises when a required key is missing on a disabled feature", async () => {
    currentView = makeView({
      features: [
        flag({
          effective_enabled: false,
          credential: { key_env: "GOOGLE_FACT_CHECK_API_KEY", key_present: false, required: true },
        }),
      ],
    });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    const note = await screen.findByText(/needs GOOGLE_FACT_CHECK_API_KEY \(not set\)/);
    expect(note).toHaveClass("key-note");
    expect(screen.queryByText(/⚠/)).not.toBeInTheDocument();
  });

  it("never treats an optional key as a fault", async () => {
    currentView = makeView({
      features: [
        flag({
          key: "MATH_SOLVE",
          label: "Precision math (SymPy)",
          effective_enabled: true,
          credential: { key_env: "WOLFRAM_ALPHA_APP_ID", key_present: false, required: false },
        }),
      ],
    });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    const note = await screen.findByText(/WOLFRAM_ALPHA_APP_ID not set \(optional\)/);
    expect(note).toHaveClass("key-note");
    expect(screen.queryByText(/⚠/)).not.toBeInTheDocument();
  });

  it("shows nothing when the key is present or the flag needs none", async () => {
    currentView = makeView({
      features: [
        flag({
          effective_enabled: true,
          credential: { key_env: "GOOGLE_FACT_CHECK_API_KEY", key_present: true, required: true },
        }),
        flag({ key: "CODE_EXECUTION", label: "Code execution", credential: null }),
      ],
    });
    render(<Settings apiBase="/api" getHeaders={headers} onClose={noop} />);

    await screen.findByText("Optional features");
    expect(screen.queryByText(/not set/)).not.toBeInTheDocument();
  });
});
