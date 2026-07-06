/**
 * Onboarding tour step definitions.
 *
 * Each step anchors to a `[data-tour="..."]` element, is gated to a route
 * pattern, and declares how it advances: a manual Next button, an app event
 * fired via `tourEvent()` (stores/tour.ts) when the user performs the step's
 * action, or a route change. The tour is intentionally action-driven — users
 * actually create a case, upload a file, and click through the Explorer
 * rather than watching a passive slideshow.
 */

export type TourEventName =
  | "case-created"
  | "upload-dialog-opened"
  | "source-uploaded"
  | "ingest-complete"
  | "event-expanded"
  | "filter-added";

export type TourAdvance =
  | { type: "next" }
  | { type: "event"; name: TourEventName }
  | { type: "route"; pattern: string };

export interface TourStep {
  id: string;
  /** react-router pattern the step's page must match (via `matchPath`). */
  routePattern: string;
  /** `[data-tour="..."]` selector(s); omit for a centered card with no
   * spotlight. An array is a priority list — the first selector with a match
   * wins, so a step can retarget as the user progresses (e.g. dropzone →
   * enabled Upload button once a file is picked). */
  selector?: string | string[];
  side: "top" | "right" | "bottom" | "left";
  title: string;
  body: string;
  advance: TourAdvance;
  /** Event-advanced step that should still offer a Next button as an out. */
  alsoNext?: boolean;
  /** Step targets content inside a Radix Dialog (overlay z-40 / content z-50). */
  aboveDialog?: boolean;
  /** Target is a dialog trigger: hide the whole tour overlay while its dialog
   * is open (Radix stamps `data-state="open"` on the trigger) so the card
   * doesn't float over the form the user is filling in. */
  hideWhileTriggerOpen?: boolean;
  /** Add `.tour-force-visible` to the target (for hover-only controls). */
  forceVisibleClass?: boolean;
}

const CASES = "/";
const CASE = "/cases/:caseId";
const EXPLORER = "/cases/:caseId/timelines/:timelineId";
const VISUALIZE = "/cases/:caseId/timelines/:timelineId/visualize";

export const TOUR_STEPS: TourStep[] = [
  {
    id: "create-case",
    routePattern: CASES,
    selector: '[data-tour="new-case"]',
    side: "bottom",
    title: "Create your first case",
    body: "Everything in TraceSignal lives inside a case — one investigation context grouping sources and timelines. Click New Case and give it a name.",
    advance: { type: "event", name: "case-created" },
    hideWhileTriggerOpen: true,
  },
  {
    id: "open-case",
    routePattern: CASES,
    selector: '[data-tour="case-list"]',
    side: "bottom",
    title: "Open the case",
    body: "Your new case appears in this list. Click it to open the case overview.",
    advance: { type: "route", pattern: CASE },
  },
  {
    id: "upload-open",
    routePattern: CASE,
    selector: '[data-tour="upload-log"]',
    side: "bottom",
    title: "Ingest a log file",
    body: "Click Upload Log File to add your first source. TraceSignal ingests Timesketch-compatible CSV and JSONL files.",
    advance: { type: "event", name: "upload-dialog-opened" },
    hideWhileTriggerOpen: true,
  },
  {
    id: "converter-hint",
    routePattern: CASE,
    selector: '[data-tour="converter-hint"]',
    side: "top",
    title: "Raw logs? Normalize them first",
    body: "If your logs aren't in Timesketch format yet (nginx, firewall, CloudTrail, browser history, journald …), expand this section to download a converter script that normalizes them offline.",
    advance: { type: "next" },
    aboveDialog: true,
  },
  {
    id: "do-upload",
    routePattern: CASE,
    // Once a file is picked the Upload button enables — move the spotlight
    // onto it so the next action is unmistakable.
    selector: ['[data-tour="upload-submit"]:not([disabled])', '[data-tour="upload-dropzone"]'],
    side: "right",
    title: "Upload the file",
    body: "Drop a normalized log file here (or click to browse), then hit Upload. Ingestion runs as a background job — you can keep working while it loads.",
    advance: { type: "event", name: "source-uploaded" },
    aboveDialog: true,
  },
  {
    id: "ingesting",
    routePattern: CASE,
    selector: '[data-tour="job-tray"]',
    side: "top",
    title: "Ingestion in progress",
    body: "TraceSignal is parsing and storing your file in the background — this tray tracks progress. The tour continues automatically once ingestion finishes.",
    advance: { type: "event", name: "ingest-complete" },
  },
  {
    id: "all-sources",
    routePattern: CASE,
    selector: '[data-tour="all-sources-timeline"]',
    side: "bottom",
    title: "Open the All sources timeline",
    body: "Ingestion is done. Every case has a default timeline aggregating all its sources — click it to explore the events.",
    advance: { type: "route", pattern: EXPLORER },
  },
  {
    id: "columns",
    routePattern: EXPLORER,
    // Once the picker is open, spotlight the dropdown itself and put the card
    // to its left (over the grid) — a bottom-anchored card would sit right on
    // top of the `align="end"` popover that opens below the button.
    selector: ['[data-tour="column-picker-content"]', '[data-tour="column-picker"]'],
    side: "left",
    title: "Adjust visible columns",
    body: "This is the Explorer — every event in the timeline. Use the Columns picker to choose which fields are shown in the grid, including any dynamic attributes your logs carry.",
    advance: { type: "next" },
  },
  {
    id: "open-event",
    routePattern: EXPLORER,
    selector: '[data-tour="event-grid"]',
    side: "top",
    title: "Inspect an event",
    body: "Click any row to open the event detail panel with all its attributes, annotations, and provenance.",
    advance: { type: "event", name: "event-expanded" },
  },
  {
    id: "filter-buttons",
    routePattern: EXPLORER,
    selector: '[data-tour="detail-filter-actions"]',
    side: "left",
    title: "Filter on field values",
    body: "Each attribute row has Filter IN (only events with this value) and Filter OUT (hide events with this value) actions. Try one to narrow the timeline down.",
    advance: { type: "event", name: "filter-added" },
    alsoNext: true,
    forceVisibleClass: true,
  },
  {
    id: "visualize",
    routePattern: EXPLORER,
    selector: '[data-tour="visualize-link"]',
    side: "bottom",
    title: "Visualize the filtered view",
    body: "The Visualize page charts exactly what you've filtered here — your current filters carry over. Click it to open the visualization.",
    advance: { type: "route", pattern: VISUALIZE },
  },
  {
    id: "done",
    routePattern: VISUALIZE,
    side: "bottom",
    title: "You're all set",
    body: "That's the core workflow: case → normalize → ingest → explore → filter → visualize. You can restart this tour anytime from Settings.",
    advance: { type: "next" },
  },
];
