/**
 * `InvestigatePanel` is the one place that knows a timeline has no events — a
 * detector view sees only its own empty response. This covers the gating that
 * follows from that: every analysis tab says so once, with somewhere to go, and
 * no tab offers a scan there would be nothing to scan.
 *
 * The Sigma tab is the sharp case and the reason this file exists. Its Run button
 * would happily scan an empty timeline and report zero matches, which reads as
 * "these rules cleared you" rather than "there was nothing to match against" —
 * the same silent-clean-result failure the detector views were fixed for.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { InvestigatePanel } from "@/components/analysis/InvestigatePanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { Source } from "@/api/types";

const listSourcesMock = vi.fn();

vi.mock("@/api/timelines", () => ({
  timelinesApi: {
    get: () => Promise.resolve({ id: "t1", name: "Default", is_stale: false }),
    listSources: (...args: unknown[]) => listSourcesMock(...args),
  },
}));
vi.mock("@/api/dispositions", () => ({
  dispositionsApi: { list: () => Promise.resolve({ dispositions: [] }) },
}));
vi.mock("@/api/health", () => ({ useCapabilities: () => ({ embeddings: false }) }));

// The tab bodies are stubs: this file asserts which of them mounts, never what
// they render. Each would otherwise pull in its own query stack.
vi.mock("@/components/analysis/SigmaPanel", () => ({
  SigmaPanel: () => <div>sigma-panel</div>,
}));
vi.mock("@/components/analysis/PatternsView", () => ({
  PatternsView: () => <div>patterns-view</div>,
}));
vi.mock("@/components/analysis/TemplatesView", () => ({
  TemplatesView: () => <div>templates-view</div>,
}));
vi.mock("@/components/analysis/FindingsFeed", () => ({
  FindingsFeed: () => <div>findings-feed</div>,
}));
vi.mock("@/components/analysis/DetectorAccordion", () => ({
  DetectorAccordion: () => <div>detector-accordion</div>,
}));
vi.mock("@/components/analysis/FrameBar", () => ({ FrameBar: () => <div>frame-bar</div> }));
vi.mock("@/components/analysis/BaselineBuilderDrawer", () => ({
  BaselineBuilderDrawer: () => null,
}));
vi.mock("@/components/analysis/WindowsNormality", () => ({ NormalValuesList: () => null }));
vi.mock("@/components/analysis/TriageBurndown", () => ({ TriageBurndown: () => null }));
vi.mock("@/components/analysis/MethodologyPanel", () => ({ MethodologyPanel: () => null }));

/** A ready source that contributed no events — the "ran, found nothing" shape. */
function emptySource(): Source {
  return {
    id: "s1",
    case_id: "c1",
    filename: "auth.log",
    status: "ready",
    event_count: 0,
    vector_count: 0,
  } as Source;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <InvestigatePanel
            caseId="c1"
            timelineId="t1"
            hasVectors={false}
            similarAnchor={null}
            onClose={() => {}}
            onSelectEvent={() => {}}
            onSimilarClose={() => {}}
          />
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("InvestigatePanel on a timeline with no events", () => {
  beforeEach(() => listSourcesMock.mockReset());

  it("offers a Sigma scan only when there is something to scan", async () => {
    listSourcesMock.mockResolvedValue([{ ...emptySource(), event_count: 12 }]);
    renderPanel();

    screen.getByRole("button", { name: /sigma/i }).click();
    await waitFor(() => expect(screen.getByText("sigma-panel")).toBeInTheDocument());
  });

  it("replaces the Sigma tab with an empty state when the timeline has no events", async () => {
    listSourcesMock.mockResolvedValue([emptySource()]);
    renderPanel();

    screen.getByRole("button", { name: /sigma/i }).click();
    await waitFor(() =>
      expect(screen.getByText("No events in this timeline yet.")).toBeInTheDocument(),
    );
    expect(screen.queryByText("sigma-panel")).not.toBeInTheDocument();
    // Guidance stays: "what would this tab do for me" is most worth answering
    // before there is data.
    expect(screen.getByText(/How Sigma scanning works/i)).toBeInTheDocument();
  });

  it("says the sources are still ingesting rather than that there is nothing", async () => {
    listSourcesMock.mockResolvedValue([{ ...emptySource(), status: "ingesting" }]);
    renderPanel();

    await waitFor(() =>
      expect(
        screen.getByText("This timeline's sources are still ingesting."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("findings-feed")).not.toBeInTheDocument();
  });

  it("links to the case overview so the empty state has an action", async () => {
    listSourcesMock.mockResolvedValue([]);
    renderPanel();

    const link = await screen.findByRole("link", { name: /case overview/i });
    expect(link).toHaveAttribute("href", "/cases/c1");
  });

  it("says it once for the whole panel, not once per tab body", async () => {
    listSourcesMock.mockResolvedValue([emptySource()]);
    renderPanel();

    await waitFor(() =>
      expect(screen.getAllByText("No events in this timeline yet.")).toHaveLength(1),
    );
    expect(screen.queryByText("frame-bar")).not.toBeInTheDocument();
  });
});
