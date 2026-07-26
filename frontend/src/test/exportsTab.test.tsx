import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { storiesApi } from "@/api/stories";
import { ExportsTab } from "@/components/stories/ExportsTab";
import type { StoryExportMeta, StorySnapshot } from "@/api/types";
import fixture from "./fixtures/story-snapshot.json";

const CASE = "c1";
const STORY = "s1";
const SNAPSHOT = fixture as unknown as StorySnapshot;

function exportMeta(overrides: Partial<StoryExportMeta> = {}): StoryExportMeta {
  return {
    id: "x1",
    story_id: STORY,
    case_id: CASE,
    snapshot_hash: "a".repeat(64),
    html_hash: null,
    has_artifact: false,
    created_by: "alice",
    created_at: "2026-07-26T12:00:00+00:00",
    ...overrides,
  };
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ExportsTab caseId={CASE} storyId={STORY} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  // jsdom has no real download plumbing; the assertions are about the API calls.
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(), revokeObjectURL: vi.fn() });
});

describe("ExportsTab", () => {
  it("re-seals an export whose artifact upload never landed", async () => {
    vi.spyOn(storiesApi, "listExports").mockResolvedValue([exportMeta()]);
    const getSnapshot = vi.spyOn(storiesApi, "getSnapshot").mockResolvedValue(SNAPSHOT);
    const upload = vi
      .spyOn(storiesApi, "uploadArtifact")
      .mockResolvedValue(exportMeta({ has_artifact: true, html_hash: "b".repeat(64) }));
    const createExport = vi.spyOn(storiesApi, "createExport");

    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: /Render HTML/ }));

    await waitFor(() => expect(upload).toHaveBeenCalled());
    expect(getSnapshot).toHaveBeenCalledWith(CASE, STORY, "x1");
    // Re-rendering must never re-resolve the story: the artifact has to attest
    // to the snapshot already stored under this hash.
    expect(createExport).not.toHaveBeenCalled();
    const [, , , html] = upload.mock.calls[0];
    expect(html).toContain("a".repeat(64));
  });

  it("offers no re-seal for an export that already carries its artifact", async () => {
    vi.spyOn(storiesApi, "listExports").mockResolvedValue([
      exportMeta({ has_artifact: true, html_hash: "b".repeat(64) }),
    ]);
    renderTab();
    expect(await screen.findByRole("link", { name: /HTML/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Render HTML/ })).not.toBeInTheDocument();
  });
});
