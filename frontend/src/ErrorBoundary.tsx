import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null };

/**
 * Catches render-time errors anywhere in the tree so a single bad render (e.g.
 * malformed markdown) shows a recoverable message instead of a blank page.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Unhandled UI error:", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="error-boundary" role="alert">
          <h1>Something went wrong</h1>
          <p>{this.state.error.message}</p>
          <button onClick={() => window.location.reload()}>Reload</button>
        </div>
      );
    }
    return this.props.children;
  }
}
