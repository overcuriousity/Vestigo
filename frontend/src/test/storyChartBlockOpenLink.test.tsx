/**
 * A chart block's "Open in Visualize" link has to reconstruct *that* chart.
 * The Visualize page holds its whole chart state in `c_*` URL params, so a
 * link that only names the timeline lands the analyst on a blank Visualize
 * page with the preset picker open — the story says "this chart", the link
 * delivers something else.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChartBlockCard } from "@/components/stories/EmbedCards";
import type { StoryBlockOf } from "@/api/types";

const listChartsMock = vi.fn();

vi.mock("@/api/viz", () => ({
  savedChartsApi: { list: (...a: unknown[]) => listChartsMock(...a) },
}));
// The canvas itself fetches and draws; this test is about the link only.
vi.mock("@/components/viz/ChartCanvas", () => ({ ChartCanvas: () => <div data-testid="canvas" /> }));

const CASE_ID = "case-1";
const TIMELINE_ID = "tl-1";

const block = {
  id: "b1",
  story_id: "s1",
  position: 0,
  origin: "user",
  kind: "chart_ref",
  content: { chart_id: "chart-1", timeline_id: TIMELINE_ID },
} as unknown as StoryBlockOf<"chart_ref">;

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ChartBlockCard block={block} caseId={CASE_ID} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ChartBlockCard open link", () => {
  beforeEach(() => {
    listChartsMock.mockReset();
    listChartsMock.mockResolvedValue({
      charts: [
        {
          id: "chart-1",
          case_id: CASE_ID,
          timeline_id: TIMELINE_ID,
          name: "Logons by host",
          config: {
            v: 1,
            chartType: "bar",
            scale: "nominal",
            field: "hostname",
            metric: "count",
          },
          created_at: null,
          updated_at: null,
        },
      ],
    });
  });

  it("carries the saved chart's config into the Visualize URL", async () => {
    renderCard();
    const link = await screen.findByRole("link", { name: /Open in Visualize/ });
    await waitFor(() => expect(screen.getByTestId("canvas")).toBeTruthy());

    const href = link.getAttribute("href")!;
    const [path, query] = href.split("?");
    expect(path).toBe(`/cases/${CASE_ID}/timelines/${TIMELINE_ID}/visualize`);

    const params = new URLSearchParams(query);
    expect(params.get("c_type")).toBe("bar");
    expect(params.get("c_scale")).toBe("nominal");
    expect(params.get("c_field")).toBe("hostname");
  });
});
