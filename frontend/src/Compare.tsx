import { useEffect, useRef, useState } from "react";
import { formatCost } from "./format";
import { useModalFocus } from "./useModalFocus";

type CompareResult = {
  model: string;
  answer: string;
  mode_used: string;
  notes: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  elapsed_ms: number;
};

function buildCompareMarkdown(question: string, results: CompareResult[]): string {
  const lines: string[] = [`# Compare: ${question}`, ""];
  for (const result of results) {
    lines.push(`## ${result.model}`);
    const meta = [
      `${result.elapsed_ms.toLocaleString()} ms`,
      formatCost(result.cost_usd),
      result.input_tokens != null || result.output_tokens != null
        ? `${(result.input_tokens ?? 0) + (result.output_tokens ?? 0)} tok`
        : null,
    ].filter(Boolean);
    lines.push(`_${meta.join(" · ")}_`, "");
    lines.push(result.answer ? result.answer : `No answer — ${result.notes}`, "", "---", "");
  }
  return lines.join("\n");
}

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  availableModels: string[];
  onClose: () => void;
  onOpenConversation: (conversationId: number) => void;
  // Called after a compare call completes successfully, so the caller can
  // refresh a spend indicator — Compare bills one call per model, so this
  // can move the running total more than a single Ask does.
  onCostIncurred?: () => void;
};

const MIN_MODELS = 2;
const MAX_MODELS = 4;

export function Compare({
  apiBase,
  getHeaders,
  availableModels,
  onClose,
  onOpenConversation,
  onCostIncurred,
}: Props) {
  const [question, setQuestion] = useState("");
  const [selectedModels, setSelectedModels] = useState<string[]>(
    availableModels.slice(0, MAX_MODELS),
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState<CompareResult[] | null>(null);
  // The question actually asked, captured from the response rather than
  // read live from `question` — the textarea can keep changing after the
  // request fires, and export/copy should describe what was actually
  // compared, not whatever's currently typed.
  const [askedQuestion, setAskedQuestion] = useState("");
  const [copied, setCopied] = useState(false);
  const [savingModel, setSavingModel] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);

  const [customModelInput, setCustomModelInput] = useState("");
  // Selected models not in the configured tier list — added by typing a
  // specific model name (e.g. one you just added a provider key for). They
  // have no checkbox, so they get their own removable chip.
  const customModels = selectedModels.filter((model) => !availableModels.includes(model));

  function toggleModel(model: string) {
    setSelectedModels((prev) => {
      if (prev.includes(model)) {
        return prev.filter((candidate) => candidate !== model);
      }
      if (prev.length >= MAX_MODELS) {
        return prev;
      }
      return [...prev, model];
    });
  }

  function addCustomModel() {
    const cleaned = customModelInput.trim();
    if (!cleaned) {
      return;
    }
    if (selectedModels.includes(cleaned)) {
      setError(`"${cleaned}" is already selected.`);
      return;
    }
    if (selectedModels.length >= MAX_MODELS) {
      setError(`You can compare at most ${MAX_MODELS} models — remove one first.`);
      return;
    }
    setSelectedModels((prev) => [...prev, cleaned]);
    setCustomModelInput("");
    setError("");
  }

  async function copyResultsAsMarkdown() {
    if (!results) {
      return;
    }
    try {
      await navigator.clipboard.writeText(buildCompareMarkdown(askedQuestion, results));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Failed to copy to clipboard.");
    }
  }

  async function saveResultAsConversation(result: CompareResult) {
    if (!result.answer) {
      return;
    }
    setSavingModel(result.model);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/conversations/import`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          title:
            askedQuestion.length > 70 ? `${askedQuestion.slice(0, 70)}…` : askedQuestion,
          pinned_model: result.model,
          messages: [
            { role: "user", content: askedQuestion },
            {
              role: "assistant",
              content: result.answer,
              mode_used: `compare:${result.model}`,
              input_tokens: result.input_tokens,
              output_tokens: result.output_tokens,
              cost_usd: result.cost_usd,
            },
          ],
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(
          typeof body.detail === "string" ? body.detail : `Failed to save conversation (${res.status})`,
        );
      }
      const conversation = (await res.json()) as { id: number };
      onOpenConversation(conversation.id);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save conversation");
    } finally {
      setSavingModel(null);
    }
  }

  async function runCompare() {
    const cleanQuestion = question.trim();
    if (!cleanQuestion) {
      setError("Enter a question first.");
      return;
    }
    if (selectedModels.length < MIN_MODELS) {
      setError(`Pick at least ${MIN_MODELS} models to compare.`);
      return;
    }

    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/compare`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ question: cleanQuestion, models: selectedModels }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(
          typeof body.detail === "string" ? body.detail : `Compare failed (${res.status})`,
        );
      }
      const data = (await res.json()) as { question: string; results: CompareResult[] };
      setResults(data.results);
      setAskedQuestion(data.question);
      onCostIncurred?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to compare models");
    } finally {
      setLoading(false);
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
        className="settings-modal compare-modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-label="Compare models"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Compare models</h2>
          <button className="link-button" onClick={onClose} aria-label="Close compare">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          Ask the same question of {MIN_MODELS}–{MAX_MODELS} models side-by-side — see how
          answers, cost, and latency actually differ across providers and tiers.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        <section className="settings-section">
          <h3>Models ({selectedModels.length} selected)</h3>
          {availableModels.length === 0 ? (
            <p className="settings-readonly">No configured tier models — add one below.</p>
          ) : (
            <div className="compare-model-picker">
              {availableModels.map((model) => (
                <label key={model} className="compare-model-option">
                  <input
                    type="checkbox"
                    checked={selectedModels.includes(model)}
                    onChange={() => toggleModel(model)}
                    disabled={!selectedModels.includes(model) && selectedModels.length >= MAX_MODELS}
                  />
                  {model}
                </label>
              ))}
            </div>
          )}

          {customModels.length > 0 ? (
            <div className="compare-custom-chips">
              {customModels.map((model) => (
                <span key={model} className="compare-custom-chip">
                  {model}
                  <button
                    type="button"
                    onClick={() => toggleModel(model)}
                    aria-label={`Remove ${model}`}
                  >
                    ✕
                  </button>
                </span>
              ))}
            </div>
          ) : null}

          <div className="compare-custom-model">
            <input
              type="text"
              value={customModelInput}
              onChange={(event) => setCustomModelInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addCustomModel();
                }
              }}
              placeholder="Add a specific model, e.g. groq/llama-3.3-70b-versatile"
              aria-label="Add a custom model"
              disabled={loading || selectedModels.length >= MAX_MODELS}
            />
            <button
              type="button"
              className="secondary-button"
              onClick={addCustomModel}
              disabled={loading || selectedModels.length >= MAX_MODELS}
            >
              Add
            </button>
          </div>
        </section>

        <section className="settings-section">
          <label htmlFor="compare-question">Question</label>
          <textarea
            id="compare-question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="e.g. Explain the CAP theorem in two sentences."
            rows={3}
            disabled={loading}
          />
          <div className="instructions-actions">
            <button
              onClick={() => void runCompare()}
              disabled={loading}
              title="Uses paid API tokens/credits (one call per model)"
            >
              {loading ? "Comparing…" : "$ Compare"}
            </button>
          </div>
        </section>

        {results ? (
          <section className="settings-section">
            <div className="usage-section-header">
              <h3>Results</h3>
              <button
                type="button"
                className="secondary-button"
                onClick={() => void copyResultsAsMarkdown()}
              >
                {copied ? "✓ Copied!" : "📋 Copy as Markdown"}
              </button>
            </div>
            <div className="compare-results">
              {results.map((result) => (
                <article key={result.model} className="compare-result-card">
                  <header>
                    <strong>{result.model}</strong>
                    <span className="compare-result-meta">
                      {result.elapsed_ms.toLocaleString()} ms
                      {formatCost(result.cost_usd) ? ` · ${formatCost(result.cost_usd)}` : ""}
                      {result.input_tokens != null || result.output_tokens != null
                        ? ` · ${(result.input_tokens ?? 0) + (result.output_tokens ?? 0)} tok`
                        : ""}
                    </span>
                  </header>
                  {result.answer ? (
                    <p className="compare-result-answer">{result.answer}</p>
                  ) : (
                    <p className="compare-result-answer compare-result-empty">
                      No answer — {result.notes}
                    </p>
                  )}
                  {result.answer ? (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => void saveResultAsConversation(result)}
                      disabled={savingModel !== null}
                    >
                      {savingModel === result.model ? "Saving…" : "💬 Continue in new conversation"}
                    </button>
                  ) : null}
                </article>
              ))}
            </div>
          </section>
        ) : null}

        <footer className="settings-footer">
          <button className="secondary-button" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}
