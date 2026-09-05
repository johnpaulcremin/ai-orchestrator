import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./Button";
import { reportClientError } from "./crashReporter.ts";

type Props = { children: ReactNode; label?: string };
type State = { error: Error | null; componentStack: string | null };

/**
 * Catches render-time errors anywhere in the tree so a single bad render (e.g.
 * malformed markdown) shows a recoverable message instead of a blank page.
 *
 * `label` (optional) names which part of the app this boundary covers (e.g.
 * "Usage panel") in the fallback heading — useful when several boundaries are
 * nested (see App.tsx, where each lazy-loaded modal gets its own boundary so
 * one panel crashing can't take down the whole app underneath it).
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Unhandled UI error:", error, info);
    // Also forward to the backend's crash log (see crashReporter.ts) —
    // a render error caught here on a phone is otherwise invisible.
    reportClientError(
      `${this.props.label ? `[${this.props.label}] ` : ""}${error.message}`,
      `${error.stack ?? ""}${info.componentStack ? `\n\nComponent stack:${info.componentStack}` : ""}`,
    );
    this.setState({ componentStack: info.componentStack ?? null });
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="error-boundary" role="alert">
          <h1>{this.props.label ? `${this.props.label}: something went wrong` : "Something went wrong"}</h1>
          <p>{this.state.error.message}</p>
          <details className="error-boundary-details">
            <summary>Show details</summary>
            <pre>
              {this.state.error.stack ?? this.state.error.message}
              {this.state.componentStack ? `\n\nComponent stack:${this.state.componentStack}` : ""}
            </pre>
          </details>
          <Button variant="primary" onClick={() => window.location.reload()}>Reload</Button>
        </div>
      );
    }
    return this.props.children;
  }
}
