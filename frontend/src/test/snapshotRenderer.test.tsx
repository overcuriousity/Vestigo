import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SnapshotRenderer } from "@/components/stories/SnapshotRenderer";
import { renderExportHtml } from "@/components/stories/exportHtml";
import { snapshotToChartResult } from "@/components/viz/chartFetch";
import { parseStoredChartConfig } from "@/components/viz/lib/chartConfig";
import type { CompareTimeResponse, StorySnapshot } from "@/api/types";
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

  it("reshapes a frozen time histogram the way the live path does", () => {
    // The server freezes an uncompared time chart as the raw histogram
    // ({start, count}); the mark reads {primary, comparison}. Passing it
    // through unmapped drew every bar as `undefined` — silently, in every
    // export, for the most common chart in an intrusion report.
    const block = snapshot.blocks.find(
      (b) => b.kind === "chart_ref" && (b.data as { name?: string })?.name === "Events over time",
    );
    expect(block).toBeDefined();
    const data = block!.data as {
      config: Record<string, unknown>;
      resolved: { data_kind: string; compare_mode: string };
      chart: unknown;
    };
    const config = parseStoredChartConfig(data.config);
    expect(config).not.toBeNull();
    const result = snapshotToChartResult(
      data.resolved.data_kind,
      data.resolved.compare_mode,
      config!,
      data.chart,
    );
    expect(result?.kind).toBe("time");
    const buckets = (result as { data: CompareTimeResponse }).data.buckets;
    expect(buckets.map((b) => b.primary)).toEqual([41, 7, 512]);
    expect(buckets.every((b) => b.primary !== undefined)).toBe(true);
  });

  it("marks agent-authored blocks in the exported report", () => {
    // The export is the artifact that leaves the tool; which paragraphs the
    // AI wrote has to survive into it, not just into the editor.
    render(<SnapshotRenderer snapshot={snapshot} />);
    expect(screen.getAllByText("agent-authored").length).toBe(
      snapshot.blocks.filter((b) => b.origin === "agent").length,
    );
  });

  it("renders a self-contained HTML document with no external references", () => {
    const html = renderExportHtml(snapshot, "a".repeat(64));
    expect(html.startsWith("<!doctype html>")).toBe(true);
    expect(html).toContain("Intrusion report");
    expect(html).toContain("Accepted password for svc_backup");
    // The artifact must name the snapshot it renders — the seal endpoint
    // refuses one that doesn't, since otherwise html_hash attests to nothing.
    expect(html).toContain("a".repeat(64));
    // No remote assets: nothing to fetch when the file is opened from disk.
    // (SVG `xmlns` namespace URIs are identifiers, not fetches, so the check
    // targets the attributes a browser would actually request.)
    expect(html).not.toMatch(/<script/i);
    expect(html).not.toMatch(/<link[^>]+href=/i);
    expect(html).not.toMatch(/(?:src|href)="https?:\/\//i);
  });

  it("draws the charts, not just the prose around them (issue #197)", () => {
    // `renderToStaticMarkup` runs no effects and jsdom's ResizeObserver stub
    // is irrelevant to it, so `ChartFrame` sat at width 0 and its
    // `{width > 0 && <svg/>}` gate emitted nothing. Every exported report
    // carried its sections and silently dropped every diagram.
    const html = renderExportHtml(snapshot, "a".repeat(64));

    const chartBlocks = snapshot.blocks.filter(
      (b) => b.kind === "chart_ref" && (b.data as { chart?: unknown } | null)?.chart != null,
    );
    expect(chartBlocks.length).toBeGreaterThan(0);

    const svgCount = (html.match(/<svg/g) ?? []).length;
    expect(svgCount).toBeGreaterThanOrEqual(chartBlocks.length);
    // Drawn geometry, not just an empty framed <svg>.
    expect(html).toMatch(/<(?:rect|path|circle|line)\b/);
  });

  it("freezes a table block as a real <table>, not an <svg>", () => {
    const tableBlock = {
      ...snapshot.blocks.find((b) => b.kind === "chart_ref")!,
      id: "blk-table",
      data: {
        name: "Users",
        config: {
          v: 2,
          chartType: "table",
          scale: "nominal",
          field: "attr:user",
          options: {},
          derive: null,
          inputs: {},
          marks: [],
        },
        resolved: { data_kind: "table", compare_mode: "off" },
        warnings: [],
        chart: {
          kind: "table",
          field: "attr:user",
          second_field: null,
          total: 3,
          distinct: 2,
          rows: [
            {
              value: "alice",
              count: 2,
              share: 2 / 3,
              first_seen: null,
              last_seen: null,
              distinct_second: null,
            },
            {
              value: "bob",
              count: 1,
              share: 1 / 3,
              first_seen: null,
              last_seen: null,
              distinct_second: null,
            },
          ],
          remainder: null,
          sort: { by: "count", dir: "desc" },
        },
      },
    };
    const withTable = { ...snapshot, blocks: [...snapshot.blocks, tableBlock] } as StorySnapshot;
    const html = renderExportHtml(withTable, "a".repeat(64));
    expect(html).toMatch(/<table[^>]*data-testid="table-figure-html"/);
    expect(html).toContain("alice");
    expect(html).toContain("66.7%");
  });
});

describe("SnapshotRenderer — marks", () => {
  it("draws a chart block's frozen marks without any fetch", () => {
    const timeBlock = snapshot.blocks.find(
      (b) => b.kind === "chart_ref" && (b.data as { name?: string })?.name === "Events over time",
    )!;
    const withMarks = {
      ...snapshot,
      blocks: [
        {
          ...timeBlock,
          id: "blk-marks",
          data: {
            ...(timeBlock.data as object),
            marks: {
              cap: 50,
              sources: [
                { index: 0, kind: "instant", label: "first", count: 1, shown: 1, overflow: false, undated: 0 },
              ],
              marks: [
                { kind: "instant", at: "2026-07-20T01:30:00+00:00", label: "first", source: 0, provenance: { kind: "analyst" } },
              ],
            },
          },
        },
      ],
    } as StorySnapshot;
    const html = renderExportHtml(withMarks, "a".repeat(64));
    expect(html).toContain("data-mark-instant");
  });
});
