import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatTimestamp } from "./format";
import {
  collectGeneratedFiles,
  generatedFileLink,
  preserveSandboxUrls,
} from "./generatedFileLinks";
import { supportsRegexLookbehind } from "./markdownSupport";
import type { SharedConversationData } from "./types";

// See markdownSupport.ts: remark-gfm crashes rendering on Safari < 16.4.
// Dropping it there degrades to plain CommonMark instead of a blank screen.
const gfmPluginsIfSupported = supportsRegexLookbehind ? [remarkGfm] : [];

const API_BASE = "/api";

function tokenFromPath(): string {
  const match = window.location.pathname.match(/^\/shared\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : "";
}

/** The public, read-only page a share link (see Share.tsx) resolves to.
 * Rendered instead of the main chat App entirely (see main.tsx) — no
 * account, no API token, no way to ask a follow-up question. Deliberately a
 * standalone component rather than reusing MessageList: that component is
 * tightly coupled to editing/streaming/bookmarking state this page has no
 * business exposing to an anonymous viewer. */
export function SharedConversation() {
  const [data, setData] = useState<SharedConversationData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const token = tokenFromPath();
    const load = async () => {
      if (!token) {
        setError("No share link found in this URL.");
        setLoading(false);
        return;
      }
      try {
        const res = await fetch(`${API_BASE}/v1/shared/${encodeURIComponent(token)}`);
        if (!res.ok) {
          throw new Error(
            res.status === 404
              ? "This share link is invalid or has expired."
              : `Failed to load this conversation (${res.status}).`,
          );
        }
        const view = (await res.json()) as SharedConversationData;
        if (!cancelled) {
          setData(view);
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load this conversation.");
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="shared-conversation-page">
      <div className="shared-conversation-banner">
        🔗 Read-only shared conversation — a snapshot, not a live chat.
      </div>

      {loading ? (
        <p className="settings-loading">Loading…</p>
      ) : error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : data ? (
        <div className="shared-conversation-body">
          <h1>{data.title}</h1>
          <div className="messages">
            {data.messages.map((message, index) => (
              <div key={index} className={`message ${message.role}`}>
                <div className="message-meta">
                  <span className="message-role">{message.role}</span>
                  <span className="message-timestamp">{formatTimestamp(message.created_at)}</span>
                </div>

                {message.role === "assistant" ? (
                  <div className="markdown-body">
                    {/* Same `a` override as MessageList: a shared snapshot carries
                        code_results (see schemas.SharedMessage) but renders no
                        download list of its own, so a file named in the prose is
                        the only way a recipient reaches it. */}
                    <ReactMarkdown
                      remarkPlugins={gfmPluginsIfSupported}
                      urlTransform={preserveSandboxUrls}
                      components={{
                        a: generatedFileLink(collectGeneratedFiles(message.code_results)),
                      }}
                    >
                      {message.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p>{message.content}</p>
                )}

                {message.images && message.images.length > 0 ? (
                  <div className="message-images">
                    {message.images.map((src, imageIndex) => (
                      <img
                        key={imageIndex}
                        src={src}
                        alt={message.role === "user" ? "Attached" : "Generated"}
                      />
                    ))}
                  </div>
                ) : null}

                {message.files && message.files.length > 0 ? (
                  <ul className="message-files" aria-label="Attached files">
                    {message.files.map((file, fileIndex) => (
                      <li key={fileIndex}>📄 {file.filename}</li>
                    ))}
                  </ul>
                ) : null}

                {message.sources && message.sources.length > 0 ? (
                  <ul className="message-sources" aria-label="Sources">
                    {message.sources.map((source, sourceIndex) => (
                      <li key={sourceIndex}>
                        <a href={source.url} target="_blank" rel="noopener noreferrer">
                          {source.title || source.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                ) : null}

                {message.code_results && message.code_results.length > 0 ? (
                  <div className="code-results">
                    {message.code_results.map((result, codeIndex) => (
                      <details key={codeIndex} className="code-result">
                        <summary>Ran code</summary>
                        <pre>
                          <code>{result.code}</code>
                        </pre>
                        {result.logs ? <pre className="code-result-logs">{result.logs}</pre> : null}
                        {result.images && result.images.length > 0 ? (
                          <div className="code-result-images">
                            {result.images.map((src, imageIndex) => (
                              <img key={imageIndex} src={src} alt="Code output" />
                            ))}
                          </div>
                        ) : null}
                      </details>
                    ))}
                  </div>
                ) : null}

                {message.fact_checks && message.fact_checks.length > 0 ? (
                  <ul className="fact-checks" aria-label="Fact checks">
                    {message.fact_checks.map((result, factIndex) => (
                      <li key={factIndex} className="fact-check">
                        {result.rating ? (
                          <span className="fact-check-rating" aria-label={`Rating: ${result.rating}`}>
                            {result.rating}
                          </span>
                        ) : null}
                        <span className="fact-check-claim">{result.claim}</span>
                        {result.url ? (
                          <a
                            className="fact-check-source"
                            href={result.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {result.publisher || result.url}
                          </a>
                        ) : result.publisher ? (
                          <span className="fact-check-source">{result.publisher}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}

                {message.academic_results && message.academic_results.length > 0 ? (
                  <ul className="academic-results" aria-label="Academic search results">
                    {message.academic_results.map((result, academicIndex) => (
                      <li key={academicIndex} className="academic-result">
                        <span className="academic-result-title">{result.title}</span>
                        {result.authors || result.year ? (
                          <span className="academic-result-meta">
                            {[result.authors, result.year].filter(Boolean).join(" · ")}
                          </span>
                        ) : null}
                        {result.url ? (
                          <a
                            className="academic-result-source"
                            href={result.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {result.venue || result.url}
                          </a>
                        ) : result.venue ? (
                          <span className="academic-result-source">{result.venue}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}

                {message.math_results && message.math_results.length > 0 ? (
                  <ul className="math-results" aria-label="Computed results">
                    {message.math_results.map((result, mathIndex) => (
                      <li key={mathIndex} className="math-result">
                        <code className="math-result-expression">{result.expression}</code>
                        {result.result ? (
                          <span className="math-result-value">
                            = {result.result}
                            {result.source === "wolfram_alpha" ? (
                              <span className="math-result-source"> (via Wolfram Alpha)</span>
                            ) : null}
                          </span>
                        ) : (
                          <span className="math-result-error" aria-label={`Error: ${result.error}`}>
                            {result.error}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </main>
  );
}
