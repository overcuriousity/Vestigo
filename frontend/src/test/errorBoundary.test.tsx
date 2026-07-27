/**
 * Containment: a component that throws during render must cost its own box
 * and nothing else. Before boundaries existed, one malformed persisted record
 * unmounted the whole app — every route, not just the panel that read it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";

function Boom({ explode }: { explode: boolean }): React.ReactElement {
  if (explode) throw new Error("cannot read 'chart_type' of a string");
  return <div>rendered fine</div>;
}

beforeEach(() => {
  // React logs the caught error itself; keep the test output readable.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ErrorBoundary", () => {
  it("renders the fallback instead of propagating, keeping siblings mounted", () => {
    render(
      <div>
        <ErrorBoundary label="This chart proposal">
          <Boom explode />
        </ErrorBoundary>
        <div>sibling content</div>
      </div>,
    );
    expect(screen.getByText(/This chart proposal could not be displayed/)).toBeTruthy();
    expect(screen.getByText(/cannot read 'chart_type' of a string/)).toBeTruthy();
    // The point of the whole exercise: the rest of the page survived.
    expect(screen.getByText("sibling content")).toBeTruthy();
  });

  it("passes children through untouched when nothing throws", () => {
    render(
      <ErrorBoundary label="This page">
        <Boom explode={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("rendered fine")).toBeTruthy();
  });

  it("clears the error when resetKey changes, so navigating away recovers", () => {
    const { rerender } = render(
      <ErrorBoundary label="This page" resetKey="/cases/a">
        <Boom explode />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/could not be displayed/)).toBeTruthy();
    rerender(
      <ErrorBoundary label="This page" resetKey="/cases/b">
        <Boom explode={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("rendered fine")).toBeTruthy();
  });

  it("uses a caller-supplied fallback when given one", () => {
    render(
      <ErrorBoundary label="x" fallback={(e) => <span>custom: {e.message}</span>}>
        <Boom explode />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/custom: cannot read/)).toBeTruthy();
  });
});
