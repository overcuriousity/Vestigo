/**
 * GuidancePanel behavior: collapsible, and the collapsed state persists per
 * panel id in localStorage (issue #11 — guidance must be permanently
 * dismissible without ever blocking).
 */
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { GuidancePanel } from "@/components/ui/GuidancePanel";

describe("GuidancePanel", () => {
  beforeEach(() => localStorage.clear());

  it("renders title and content expanded by default", () => {
    render(
      <GuidancePanel id="t1" title="How this works">
        <p>hello guidance</p>
      </GuidancePanel>,
    );
    expect(screen.getByText("How this works")).toBeInTheDocument();
    expect(screen.getByText("hello guidance")).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
  });

  it("collapses on click and persists the state", () => {
    render(
      <GuidancePanel id="t2" title="Workflow">
        <p>steps</p>
      </GuidancePanel>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText("steps")).not.toBeInTheDocument();
    expect(localStorage.getItem("tsig-guidance-t2")).toBe("collapsed");
  });

  it("starts collapsed when localStorage says so", () => {
    localStorage.setItem("tsig-guidance-t3", "collapsed");
    render(
      <GuidancePanel id="t3" title="Workflow">
        <p>steps</p>
      </GuidancePanel>,
    );
    expect(screen.queryByText("steps")).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("re-expands and clears the persisted state", () => {
    localStorage.setItem("tsig-guidance-t4", "collapsed");
    render(
      <GuidancePanel id="t4" title="Workflow">
        <p>steps</p>
      </GuidancePanel>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("steps")).toBeInTheDocument();
    expect(localStorage.getItem("tsig-guidance-t4")).toBeNull();
  });

  it("keeps collapse state independent per panel id", () => {
    localStorage.setItem("tsig-guidance-a", "collapsed");
    render(
      <>
        <GuidancePanel id="a" title="Panel A">
          <p>content-a</p>
        </GuidancePanel>
        <GuidancePanel id="b" title="Panel B">
          <p>content-b</p>
        </GuidancePanel>
      </>,
    );
    expect(screen.queryByText("content-a")).not.toBeInTheDocument();
    expect(screen.getByText("content-b")).toBeInTheDocument();
  });
});
