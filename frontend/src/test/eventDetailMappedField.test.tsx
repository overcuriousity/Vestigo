/**
 * The detail panel must say which attributes the source file did not carry.
 *
 * The query layer coalesces a timeline's canonical fields into every presented
 * row's `attributes` (db/field_mappings.py::project_mapped_fields), so a
 * canonical key sits there indistinguishably from an ingested one. The panel
 * labels it, names the raw fields behind it, and addresses it by its canonical
 * token rather than the `attr:` escape hatch (which bypasses mappings).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EventDetailPanel } from "@/components/explorer/EventDetailPanel";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { Event } from "@/api/types";

vi.mock("@/hooks/useAnnotationMutations", () => ({
  useAnnotationMutations: () => ({
    add: { mutate: vi.fn() },
    remove: { mutate: vi.fn() },
  }),
}));
vi.mock("@/hooks/useDisposition", () => ({
  useDisposition: () => ({ markNormal: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useUserNames", () => ({ useUserNames: () => ({}) }));
vi.mock("@/components/stories/AddToStoryButton", () => ({
  AddToStoryButton: () => null,
}));

const EVENT: Event = {
  event_id: "e1",
  case_id: "c1",
  source_id: "s1",
  source_file: "a.csv",
  byte_offset: 0,
  line_number: 1,
  content_hash: "ch",
  file_hash: "fh",
  parser_name: "test",
  parser_version: "1",
  ingest_time: "2026-01-01T10:00:00Z",
  message: "hello",
  timestamp: "2026-01-01T10:00:00Z",
  timestamp_desc: "Test Time",
  artifact: "test:artifact",
  artifact_long: "test:artifact",
  display_name: "a.csv",
  tags: [],
  attributes: { src_ip: "10.0.0.4", ip_address: "10.0.0.4", status: "200" },
  mapped_fields: ["ip_address"],
} as unknown as Event;

/**
 * Same row, but `ip_address` was carried by the source file — the query layer
 * never overwrites a stored key, so it reports nothing as mapped.
 */
const EVENT_WITH_STORED_CANONICAL: Event = {
  ...EVENT,
  mapped_fields: undefined,
} as unknown as Event;

function renderPanel(
  fieldMappings: Record<string, string[]> | null,
  event: Event = EVENT,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <EventDetailPanel
          event={event}
          annotations={[]}
          caseId="c1"
          timelineId="t1"
          sourceId="s1"
          onClose={() => {}}
          onFindSimilar={() => {}}
          onAddFilter={() => {}}
          fieldMappings={fieldMappings}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("EventDetailPanel mapped-field disclosure", () => {
  it("marks a canonical field and names the raw fields behind it", () => {
    renderPanel({ ip_address: ["src_ip", "ip_addr"] });
    const note = screen.getByText("mapped");
    expect(note).toBeTruthy();
    expect(note.getAttribute("title")).toContain("src_ip, ip_addr");
  });

  it("marks nothing when the timeline has no mappings", () => {
    renderPanel(null);
    expect(screen.queryByText("mapped")).toBeNull();
  });

  it("marks nothing when the source file itself carried the canonical key", () => {
    // The mapping exists, but this row's value is ingested — badging it would
    // claim a provenance the source file contradicts.
    renderPanel({ ip_address: ["src_ip", "ip_addr"] }, EVENT_WITH_STORED_CANONICAL);
    expect(screen.queryByText("mapped")).toBeNull();
  });
});
