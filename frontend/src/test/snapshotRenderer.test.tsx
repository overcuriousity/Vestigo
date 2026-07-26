import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SnapshotRenderer } from "@/components/stories/SnapshotRenderer";
import { renderExportHtml } from "@/components/stories/exportHtml";
import type { StorySnapshot } from "@/api/types";
import fixture from "./fixtures/story-snapshot.json";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

const snapshot = fixture as unknown as StorySnapshot;

// Charts gate on a measured container width; jsdom lays nothing out.
installFakeResizeObserver();

describe("SnapshotRenderer", () => {
  it("renders every block kind from snapshot data alone, with zero fetches", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<SnapshotRenderer snapshot={snapshot} />);

    // Header
    expect(screen.getByText("Intrusion report")).toBeInTheDocument();
    // Markdown
    expect(screen.getByText("Initial access")).toBeInTheDocument();
    // View rows + the honesty caption
    // Thousands separator is locale-dependent; the claim is the ratio.
    expect(screen.getByText(/2 of 14.203 rows shown/)).toBeInTheDocument();
    expect(
      screen.getByText("Failed password for invalid user admin"),
    ).toBeInTheDocument();
    // Event evidence card
    expect(screen.getByText("first successful login")).toBeInTheDocument();
    // A block the server could not resolve stays visible as a gap
    expect(screen.getByText(/unresolved at export/)).toBeInTheDocument();
    // The chart block draws from the frozen aggregation, not a refetch
    expect(screen.getByText("Top source IPs")).toBeInTheDocument();
    expect(screen.getByText("203.0.113.9")).toBeInTheDocument();

    // The whole point: a snapshot renders offline.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders a self-contained HTML document with no external references", () => {
    const html = renderExportHtml(snapshot);
    expect(html.startsWith("<!doctype html>")).toBe(true);
    expect(html).toContain("Intrusion report");
    expect(html).toContain("Accepted password for svc_backup");
    // No remote assets: nothing to fetch when the file is opened from disk.
    // (SVG `xmlns` namespace URIs are identifiers, not fetches, so the check
    // targets the attributes a browser would actually request.)
    expect(html).not.toMatch(/<script/i);
    expect(html).not.toMatch(/<link[^>]+href=/i);
    expect(html).not.toMatch(/(?:src|href)="https?:\/\//i);
  });
});
