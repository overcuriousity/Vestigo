/**
 * TemplatesView (W6) smoke test: renders the template list, splits
 * active/muted by the shared routine-dispositions cache, and posts the
 * right disposition shape on mute (detector="log_template",
 * field="template_id", value=template_id, details snapshot).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TemplatesView } from "@/components/analysis/TemplatesView";
import type { LogTemplatesResponse, DispositionListResponse } from "@/api/types";

const logTemplatesMock = vi.fn();
const fieldsMock = vi.fn();
const dispositionsListMock = vi.fn();
const dispositionsCreateMock = vi.fn();
const dispositionsRemoveMock = vi.fn();

vi.mock("@/api/anomalies", () => ({
  anomaliesApi: {
    logTemplates: (...args: unknown[]) => logTemplatesMock(...args),
    fields: (...args: unknown[]) => fieldsMock(...args),
  },
}));
vi.mock("@/api/dispositions", () => ({
  dispositionsApi: {
    list: (...args: unknown[]) => dispositionsListMock(...args),
    create: (...args: unknown[]) => dispositionsCreateMock(...args),
    remove: (...args: unknown[]) => dispositionsRemoveMock(...args),
  },
}));
vi.mock("@/stores/toasts", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const CASE = "c1";
const TL = "t1";

const TEMPLATES: LogTemplatesResponse = {
  field: "message",
  total_templates: 2,
  templates: [
    {
      template_id: "111",
      template: "Allow TCP <IP>:<NUM> -> <IP>:<NUM>",
      count: 3,
      distinct_sources: 1,
      first_seen: "2026-01-01T00:00:00Z",
      last_seen: "2026-01-02T00:00:00Z",
      example: "Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443",
    },
    {
      template_id: "222",
      template: "Deny UDP <IP>:<NUM> -> <IP>:<NUM> (spoofed-src flag)",
      count: 1,
      distinct_sources: 1,
      first_seen: "2026-01-01T00:00:00Z",
      last_seen: "2026-01-01T00:00:00Z",
      example: "Deny UDP 185.220.101.4:0 -> 10.0.0.9:3389 (spoofed-src flag)",
    },
  ],
};

function emptyDispositions(): DispositionListResponse {
  return { dispositions: [] };
}

function renderView(onDrillField?: (field: string, value: string) => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TemplatesView caseId={CASE} timelineId={TL} onDrillField={onDrillField} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  logTemplatesMock.mockResolvedValue(TEMPLATES);
  fieldsMock.mockResolvedValue({ fields: [] });
  dispositionsListMock.mockResolvedValue(emptyDispositions());
  dispositionsCreateMock.mockResolvedValue({ disposition: { id: "d1" } });
  dispositionsRemoveMock.mockResolvedValue({ deleted: true, disposition_id: "d1" });
});

describe("TemplatesView", () => {
  it("renders both templates with counts", async () => {
    renderView();
    await screen.findByText(/Allow TCP/);
    expect(screen.getByText(/Deny UDP/)).toBeTruthy();
    // Counts render as "×" + a <strong> child — match the composed text.
    expect(screen.getByText((_, el) => el?.textContent === "×3")).toBeTruthy();
    expect(screen.getByText((_, el) => el?.textContent === "×1")).toBeTruthy();
  });

  it("mutes a template with the correct disposition shape", async () => {
    renderView();
    await screen.findByText(/Allow TCP/);
    const muteButtons = screen.getAllByTitle(/^Mute:/);
    fireEvent.click(muteButtons[0]);

    await waitFor(() => expect(dispositionsCreateMock).toHaveBeenCalled());
    const body = dispositionsCreateMock.mock.calls[0][2];
    expect(body.kind).toBe("routine");
    expect(body.detector).toBe("log_template");
    expect(body.field).toBe("template_id");
    expect(body.value).toBe("111");
    expect(body.details).toMatchObject({
      template: "Allow TCP <IP>:<NUM> -> <IP>:<NUM>",
      template_version: 1,
      field: "message",
      example: "Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443",
      count_at_mute: 3,
    });
  });

  it("splits muted templates into their own section by detector=log_template", async () => {
    dispositionsListMock.mockResolvedValue({
      dispositions: [
        {
          id: "d1",
          case_id: CASE,
          timeline_id: TL,
          kind: "routine",
          detector: "log_template",
          field: "template_id",
          value: "111",
          source_id: null,
          event_id: null,
          note: null,
          details: { template: "Allow TCP <IP>:<NUM> -> <IP>:<NUM>", template_version: 1 },
          created_by: null,
          created_at: null,
        },
        {
          id: "d2",
          case_id: CASE,
          timeline_id: TL,
          kind: "routine",
          detector: "sequence_motif",
          field: "artifact",
          value: "a → b",
          source_id: null,
          event_id: null,
          note: null,
          details: { values: ["a", "b"] },
          created_by: null,
          created_at: null,
        },
      ],
    });
    renderView();
    await screen.findByText(/Muted templates \(1\)/);
    // The sequence_motif routine row must not leak into this view's muted list.
    expect(screen.queryByText(/a → b/)).toBeNull();
  });

  it("calls onDrillField with template_id when the filter action is clicked", async () => {
    const onDrillField = vi.fn();
    renderView(onDrillField);
    await screen.findByText(/Allow TCP/);
    const filterButtons = screen.getAllByTitle(/Filter the grid to this template/);
    fireEvent.click(filterButtons[0]);
    expect(onDrillField).toHaveBeenCalledWith("template_id", "111");
  });

  it("offers no mute action for a non-message field", async () => {
    // The grid's collapse predicate only knows the message template hash, so
    // an attr:* shape has nothing to collapse — browsing stays, muting goes.
    fieldsMock.mockResolvedValue({ fields: [{ token: "attr:raw_line" }] });
    renderView();
    await screen.findByText(/Allow TCP/);
    expect(screen.getAllByTitle(/^Mute:/).length).toBe(2);

    // The field picker is a combobox showing the raw token, and typing is a
    // draft until Enter commits it.
    const fieldCombo = screen.getByRole("combobox", { name: "Field" });
    fireEvent.change(fieldCombo, { target: { value: "attr:raw_line" } });
    fireEvent.keyDown(fieldCombo, { key: "Enter" });
    await waitFor(() =>
      expect(screen.getAllByTitle(/only available for Message templates/).length).toBe(2),
    );
    expect(screen.queryByTitle(/^Mute:/)).toBeNull();
  });

  it("lists a muted template that has fallen off the current page, using its snapshot", async () => {
    // Muted shapes are driven by the dispositions, not the listing — otherwise
    // a mute outside the top-N would be impossible to reverse from this tab.
    dispositionsListMock.mockResolvedValue({
      dispositions: [
        {
          id: "d9",
          case_id: CASE,
          timeline_id: TL,
          kind: "routine",
          detector: "log_template",
          field: "template_id",
          value: "999",
          source_id: null,
          event_id: null,
          note: null,
          details: {
            template: "Rotated log segment <NUM>",
            template_version: 1,
            count_at_mute: 4200,
          },
          created_by: null,
          created_at: null,
        },
      ],
    });
    renderView();
    await screen.findByText(/Muted templates \(1\)/);
    expect(screen.getByText(/Rotated log segment/)).toBeTruthy();
    // The count comes from details.count_at_mute and is labelled as a snapshot,
    // since the shape is not on the current page to re-count.
    // toLocaleString is locale-dependent (4,200 / 4.200), so match on the
    // digits and the "at mute" label rather than a fixed separator.
    expect(screen.getByTitle(/Count recorded when this template was muted/).textContent).toMatch(
      /4.200 at mute/,
    );

    fireEvent.click(screen.getByTitle(/^Unmute/));
    await waitFor(() => expect(dispositionsRemoveMock).toHaveBeenCalledWith(CASE, TL, "d9"));
  });
});
