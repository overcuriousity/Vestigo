/**
 * GuidancePanel behavior: collapsible per panel id, and — since the audit of
 * 2026-07-30 — restorable. Issue #11 asked for guidance that never blocks; the
 * first implementation read one localStorage flag into `useState` at mount,
 * which made dismissal a one-way door with no handle on the other side. Collapse
 * state now lives in the UI store so `resetGuidance()` re-expands panels that are
 * already on screen, which is the behaviour the Settings control depends on.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { guidance } from "@/lib/guidance";
import { useUiStore } from "@/stores/ui";

// Real registry ids — the panel takes no copy props, so there are no synthetic
// ones to test with any more. That is the point: copy cannot be inlined.
const A = "cases-page";
const B = "investigate-sigma";

describe("GuidancePanel", () => {
  beforeEach(() => {
    localStorage.clear();
    useUiStore.getState().resetGuidance();
  });

  it("renders the registry's title and body, expanded by default", () => {
    render(<GuidancePanel id={A} />);
    expect(screen.getByText(guidance[A].title)).toBeInTheDocument();
    expect(screen.getByText(/A case is the investigation container/)).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
  });

  it("collapses on click and records it in the store", () => {
    render(<GuidancePanel id={A} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText(/A case is the investigation container/)).not.toBeInTheDocument();
    expect(useUiStore.getState().collapsedGuidance[A]).toBe(true);
  });

  it("starts collapsed when the store says so", () => {
    useUiStore.getState().setGuidanceCollapsed(A, true);
    render(<GuidancePanel id={A} />);
    expect(screen.queryByText(/A case is the investigation container/)).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps collapse state independent per panel id", () => {
    useUiStore.getState().setGuidanceCollapsed(A, true);
    render(
      <>
        <GuidancePanel id={A} />
        <GuidancePanel id={B} />
      </>,
    );
    expect(screen.queryByText(/A case is the investigation container/)).not.toBeInTheDocument();
    expect(screen.getByText(/Sigma rules are community-standard/)).toBeInTheDocument();
  });

  // The behaviour change. Under the old `useState`-at-mount read this assertion
  // failed: storage was cleared but the mounted panel stayed collapsed until it
  // remounted, so "Show guidance again" appeared to do nothing.
  it("re-expands a mounted panel when guidance is reset", () => {
    useUiStore.getState().setGuidanceCollapsed(A, true);
    render(<GuidancePanel id={A} />);
    expect(screen.queryByText(/A case is the investigation container/)).not.toBeInTheDocument();

    // Outside an event handler, so React needs an explicit flush.
    act(() => useUiStore.getState().resetGuidance());

    expect(screen.getByText(/A case is the investigation container/)).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
  });
});
