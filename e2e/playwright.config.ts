import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..");

// A local dev checkout has a venv with every dep already installed; CI
// installs straight into the runner's system Python instead (see
// .github/workflows/ci.yml's e2e job) -- fall back to the bare interpreter
// when no venv is present rather than assuming one exists.
const venvPython = path.join(
  repoRoot,
  "venv",
  process.platform === "win32" ? "Scripts\\python.exe" : "bin/python",
);
const pythonBin = existsSync(venvPython) ? venvPython : process.platform === "win32" ? "python" : "python3";

const backendEnv = {
  OPENAI_API_KEY: "sk-e2e-stub",
  OPENAI_BASE_URL: "http://127.0.0.1:8999/v1",
  // Forced to gpt-5 for every tier so routing can never fall through to a
  // real provider even if a test forgets to pin a model -- the stub is the
  // only thing that can ever answer here.
  OPENAI_MODEL: "gpt-5",
  OPENAI_MODEL_ROUTER: "gpt-5",
  OPENAI_MODEL_FAST: "gpt-5",
  OPENAI_MODEL_SMART: "gpt-5",
  OPENAI_MODEL_BUDGET: "",
  OPENAI_MODEL_FALLBACK: "",
  GEMINI_API_KEY: "",
  ANTHROPIC_API_KEY: "",
  MISTRAL_API_KEY: "",
  JWT_SECRET: "e2e-smoke-test-secret-not-for-real-use",
  API_AUTH_TOKEN: "",
  ALLOW_REGISTRATION: "true",
  DATABASE_PATH: path.join(repoRoot, "e2e", ".e2e.db"),
  RESPONSE_CACHE: "false",
  SEMANTIC_CACHE: "false",
  SUMMARIZE_HISTORY: "false",
  RATE_LIMIT: "",
  DAILY_BUDGET_USD: "",
  ALLOWED_ORIGINS: "http://127.0.0.1:4183",
};

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "line" : "html",
  use: {
    baseURL: "http://127.0.0.1:4183",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `${pythonBin} e2e/stub_provider.py 8999`,
      cwd: repoRoot,
      url: "http://127.0.0.1:8999/",
      reuseExistingServer: !process.env.CI,
    },
    {
      command: `${pythonBin} -m uvicorn app.main:app --host 127.0.0.1 --port 8010`,
      cwd: repoRoot,
      url: "http://127.0.0.1:8010/health",
      reuseExistingServer: !process.env.CI,
      env: backendEnv,
    },
    {
      command: "npm run preview -- --port 4183 --strictPort --host 127.0.0.1",
      cwd: path.join(repoRoot, "frontend"),
      url: "http://127.0.0.1:4183",
      reuseExistingServer: !process.env.CI,
      env: { VITE_API_PROXY_TARGET: "http://127.0.0.1:8010" },
    },
  ],
});
