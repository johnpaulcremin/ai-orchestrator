import { useRef, useState } from "react";
import { X } from "lucide-react";
import { Button } from "./Button";
import { PRESETS, TIER_KEYS } from "./setupPresets";
import { useModalFocus } from "./useModalFocus";

// Three checkpoints, each a section heading so the app's own self-description
// (app/codebase_inventory.py reads every panel's h2 and h3 headings) lists
// what this panel can do. That reader is a plain regex over the source, so a
// literal tag inside a comment here would be taken for a heading — which is
// exactly what happened once, and why this comment spells the tags out.
//
// The key step verifies and INSTRUCTS; it never saves. That is not a gap
// waiting to be filled: app/settings.py states that credential keys are
// deliberately absent from every settable-key tuple so the settings API can
// never write or read back a secret, and a .env write from here would not
// take effect until a restart anyway (load_dotenv runs once; the OpenAI
// client is cached per process). So the honest flow is: test the pasted key,
// show the exact line to put in .env, say a restart is needed.
//
// The preset step is the opposite: model overrides ARE runtime-persistable
// and take effect on the next request, so "apply" here does apply.

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  onChanged: () => void;
  credentialsConfigured: boolean;
};

type TestOutcome = {
  ok: boolean;
  outcome: "ok" | "auth_failed" | "unreachable" | "rate_limited" | "error";
  model: string;
  key_env: string;
  detail: string;
};

function maskKey(key: string): string {
  const trimmed = key.trim();
  if (trimmed.length <= 8) return "•".repeat(trimmed.length);
  return `${trimmed.slice(0, 4)}${"•".repeat(Math.min(trimmed.length - 8, 24))}${trimmed.slice(-4)}`;
}

export function SetupWizard({
  apiBase,
  getHeaders,
  onClose,
  onChanged,
  credentialsConfigured,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(dialogRef);

  const [apiKey, setApiKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestOutcome | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const [presetId, setPresetId] = useState<string>(PRESETS[0].id);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

  const [copied, setCopied] = useState(false);

  const envLine = `OPENAI_API_KEY=${apiKey.trim()}`;

  async function testKey() {
    if (!apiKey.trim()) return;
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      const res = await fetch(`${apiBase}/v1/setup/test-key`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ api_key: apiKey.trim() }),
      });
      if (res.status === 401) {
        setTestError("Sign in first — testing a key needs an authenticated session on this deployment.");
        return;
      }
      if (!res.ok) {
        setTestError(`Test failed (${res.status}).`);
        return;
      }
      setTestResult((await res.json()) as TestOutcome);
    } catch {
      setTestError("Could not reach the backend.");
    } finally {
      setTesting(false);
    }
  }

  async function copyEnvLine() {
    try {
      await navigator.clipboard.writeText(envLine);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  async function applyPreset() {
    const preset = PRESETS.find((p) => p.id === presetId);
    if (!preset) return;
    setApplying(true);
    setApplied(null);
    setApplyError(null);
    // One PUT per key, same as the Settings panel's bulk actions: a failure
    // on one key does not block the rest, and the count says what landed.
    let ok = 0;
    let firstError: string | null = null;
    for (const key of TIER_KEYS) {
      try {
        const res = await fetch(`${apiBase}/v1/settings/${key}`, {
          method: "PUT",
          headers: getHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ value: preset.values[key] }),
        });
        if (res.ok) {
          ok += 1;
        } else if (!firstError) {
          firstError =
            res.status === 403
              ? "Settings are read-only on this deployment (ALLOW_SETTINGS_WRITE=false, or you are not an admin)."
              : `Saving ${key} failed (${res.status}).`;
        }
      } catch {
        if (!firstError) firstError = "Could not reach the backend.";
      }
    }
    setApplying(false);
    if (ok === TIER_KEYS.length) {
      setApplied(`Applied "${preset.label}" — takes effect on your next question, no restart needed.`);
      onChanged();
    } else {
      setApplied(ok > 0 ? `Applied ${ok} of ${TIER_KEYS.length} settings.` : null);
      setApplyError(firstError ?? "Nothing was saved.");
    }
  }

  return (
    <div
      className="settings-overlay"
      role="presentation"
      onClick={onClose}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="settings-modal setup-wizard-modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-label="First-run setup"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>First-run setup</h2>
          <Button
            iconOnly
            size="sm"
            variant="ghost"
            onClick={onClose}
            aria-label="Close setup"
            icon={<X size={18} />}
          />
        </header>

        <p className="settings-intro">
          {credentialsConfigured
            ? "Your API key is configured. You can still pick a model preset here."
            : "This app needs one API key before it can answer anything. Three steps, and the last one is a restart."}
        </p>

        <section className="setup-step">
          <h3>Add your API key</h3>
          <p className="setup-step-help">
            Paste an OpenAI key and test it. The key is used for one cheap call and{" "}
            <strong>never stored</strong> — this app reads it only from your <code>.env</code>.
          </p>
          <div className="setup-key-row">
            <input
              type="password"
              className="setup-key-input"
              aria-label="OpenAI API key"
              placeholder="sk-..."
              autoComplete="off"
              spellCheck={false}
              value={apiKey}
              onChange={(event) => {
                setApiKey(event.target.value);
                setTestResult(null);
                setTestError(null);
              }}
            />
            <Button
              onClick={() => void testKey()}
              disabled={testing || !apiKey.trim()}
              aria-label="Test API key"
            >
              {testing ? "Testing…" : "Test key"}
            </Button>
          </div>
          {testError ? <p className="setup-result setup-result-bad">{testError}</p> : null}
          {testResult ? (
            <p className={`setup-result ${testResult.ok ? "setup-result-good" : "setup-result-bad"}`}>
              {testResult.ok ? "✓ " : "✗ "}
              {testResult.detail}
            </p>
          ) : null}
          {testResult?.ok && apiKey.trim() ? (
            <div className="setup-env-block">
              <p className="setup-step-help">
                Put this line in <code>.env</code> in the project folder (copy it — the box shows a
                masked preview):
              </p>
              <div className="setup-env-row">
                <code className="setup-env-line" aria-label="Line to add to .env">
                  OPENAI_API_KEY={maskKey(apiKey)}
                </code>
                <Button onClick={() => void copyEnvLine()} aria-label="Copy .env line">
                  {copied ? "Copied" : "Copy"}
                </Button>
              </div>
            </div>
          ) : null}
        </section>

        <section className="setup-step">
          <h3>Choose a model preset</h3>
          <p className="setup-step-help">
            All three use only OpenAI models, so one key covers everything. You can change any
            tier later in Settings.
          </p>
          <div className="setup-presets" role="radiogroup" aria-label="Model preset">
            {PRESETS.map((preset) => (
              <label key={preset.id} className="setup-preset">
                <input
                  type="radio"
                  name="setup-preset"
                  value={preset.id}
                  checked={presetId === preset.id}
                  onChange={() => setPresetId(preset.id)}
                />
                <span>
                  <strong>{preset.label}</strong>
                  <span className="setup-preset-blurb">{preset.blurb}</span>
                </span>
              </label>
            ))}
          </div>
          <Button
            onClick={() => void applyPreset()}
            disabled={applying}
            aria-label="Apply model preset"
          >
            {applying ? "Applying…" : "Apply preset"}
          </Button>
          {applied ? <p className="setup-result setup-result-good">{applied}</p> : null}
          {applyError ? <p className="setup-result setup-result-bad">{applyError}</p> : null}
        </section>

        <section className="setup-step">
          <h3>Restart and finish</h3>
          <p className="setup-step-help">
            {credentialsConfigured
              ? "Nothing to restart — the key is already loaded."
              : "The key is read once, at startup. After adding the line to .env, restart the backend and reload this page. The preset needs no restart."}
          </p>
          <Button onClick={onClose} aria-label="Finish setup">
            Done
          </Button>
        </section>
      </div>
    </div>
  );
}
