/**
 * Render-error containment.
 *
 * React unmounts the entire tree when a render throws and nothing catches it,
 * so before this existed one malformed record — an agent tool argument a
 * provider stringified, say — took down every route including Explorer and
 * Cases. Containment is layered: `AppShell` wraps its `Outlet` so a page
 * failure keeps the shell navigable, and individual cards that render
 * model-authored data wrap themselves so one bad card does not cost the
 * conversation around it.
 *
 * A boundary is the net, not the fix — anything that trips one is a bug in
 * the data path that should also be fixed there.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

interface Props {
  children: ReactNode;
  /** Names what failed, for the console record and the default fallback. */
  label: string;
  /** Replaces the default inline notice. Receives the thrown error. */
  fallback?: (error: Error) => ReactNode;
  /**
   * Changing this clears the error — children are *not* remounted, so their
   * own state survives. Pass the route path (or any identity of what is being
   * rendered) so navigating away from a broken view recovers instead of
   * staying stuck on the fallback.
   */
  resetKey?: string;
}

interface State {
  error: Error | null;
  /** The `resetKey` the current error was recorded under; see below. */
  resetKey?: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  /** Clears the error when `resetKey` changes, during the render that
   * observes the change — rather than setting state from componentDidUpdate,
   * which would render the stale fallback once before replacing it. */
  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey === state.resetKey) return null;
    return { error: null, resetKey: props.resetKey };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Airgapped deployments have no error reporting service; the browser
    // console is the whole diagnostic trail, so make it a complete one.
    console.error(`[${this.props.label}] render failed`, error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error);
    return (
      <div className="flex items-start gap-1.5 rounded-md border border-[var(--color-danger)] bg-[var(--color-bg-elevated)] px-2.5 py-2 text-xs text-[var(--color-fg-secondary)]">
        <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--color-danger)]" />
        <div className="min-w-0">
          <div className="font-semibold text-[var(--color-fg-primary)]">
            {this.props.label} could not be displayed
          </div>
          <div className="mt-0.5 break-words font-mono text-[11px]">{error.message}</div>
          <div className="mt-0.5">
            The rest of the page is unaffected. Details are in the browser console.
          </div>
        </div>
      </div>
    );
  }
}
