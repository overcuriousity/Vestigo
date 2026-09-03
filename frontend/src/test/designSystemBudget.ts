/**
 * Per-file violation budget for `designSystem.test.ts` — a **migration artifact**,
 * not a permanent fixture. Its end state is `{}`.
 *
 * Every other coverage assertion in this repo derives its set rather than listing
 * it (`tests/test_settings_api.py` enumerates `Settings.model_fields`;
 * `tests/test_chart_meta.py` derives `KNOWN_OPTION_KEYS` and says so in its
 * docstring). This is the first hand-seeded exemption list, and it exists for one
 * reason: the two checks it covers have no fix available yet. Arbitrary font sizes
 * cannot be replaced until a type scale exists, and raw `<button>`s cannot be
 * replaced until `IconButton` does — both are `docs/ROADMAP.md` Milestone 3 items
 * under "Frontend design-system consistency". The budget stops the counts growing
 * in the meantime, and the test forces every entry down as those land.
 *
 * The numbers only ever go down. Lowering one is the whole point; raising one
 * needs a reason in the commit message.
 */

export interface FileBudget {
  /** Arbitrary Tailwind font sizes: `text-[11px]`. Replaced by the type scale. */
  fontSize?: number;
  /** Raw `<button>` outside `components/ui/`. Replaced by `Button`/`IconButton`. */
  rawButton?: number;
}

export const BUDGET: Record<string, FileBudget> = {
  "../components/agent/AgentFiltersBar.tsx": { fontSize: 1 },
  "../components/agent/AgentPanel.tsx": { fontSize: 14 },
  "../components/agent/ChartProposalCard.tsx": { fontSize: 2 },
  "../components/agent/FindingCard.tsx": { fontSize: 1 },
  "../components/agent/Markdown.tsx": { fontSize: 1 },
  "../components/agent/ProposalCard.tsx": { fontSize: 3 },
  "../components/agent/StoryBlockProposalCard.tsx": { fontSize: 2 },
  "../components/agent/ToolSelector.tsx": { fontSize: 8, rawButton: 2 },
  // Mounted again by the method sheet's `fields` knob, which was a text box
  // between the rail-plus-overlay refactor and this fix.
  "../components/analysis/AnomalyFieldPicker.tsx": { fontSize: 1, rawButton: 4 },
  "../components/analysis/FindingGroup.tsx": { fontSize: 3, rawButton: 1 },
  // +1 each for the show-dismissed toggle, which is deliberately the same
  // pill as the preset filters it sits beside — a `Button` there would read as
  // a different kind of control than the row it belongs to.
  "../components/analysis/InvestigateRail.tsx": { fontSize: 5, rawButton: 3 },
  "../components/analysis/InvestigateSheet.tsx": { fontSize: 4 },
  // +1 button for the per-method mute toggle, which is the same dimmed-at-rest
  // icon affordance as the Settings2 one beside it — a `Button` there would be
  // visually heavier than the row it acts on, the same argument SigmaFindings
  // and FindingGroup carry.
  "../components/analysis/MethodRow.tsx": { fontSize: 3, rawButton: 4 },
  "../components/analysis/ScopeStrip.tsx": { fontSize: 1, rawButton: 1 },
  // +1 for the log-template blurb, matching the motif blurb above it.
  // +1 button for the section tab, which is a bottom-border tab rather than
  // anything `Button` has a variant for.
  "../components/analysis/ToolsSheet.tsx": { fontSize: 8, rawButton: 3 },
  "../components/analysis/PatternsView.tsx": { fontSize: 1, rawButton: 3 },
  "../components/analysis/SigmaPanel.tsx": { fontSize: 7, rawButton: 5 },
  // Same two allowances FindingGroup carries, for the same reason: these rows
  // sit in the same list as its rows and must read as the same kind of thing —
  // one 10px detector chip, and one dimmed-at-rest hover action where a
  // `Button` would be a visually heavier control than the row it acts on.
  "../components/analysis/SigmaFindings.tsx": { fontSize: 1, rawButton: 1 },
  "../components/analysis/SimilarEvents.tsx": { rawButton: 1 },
  "../components/analysis/TemplatesView.tsx": { rawButton: 3 },
  "../components/analysis/WindowsNormality.tsx": { fontSize: 7, rawButton: 7 },
  "../components/analysis/detector-shared.tsx": { fontSize: 3, rawButton: 10 },
  "../components/cases/ImportCaseDialog.tsx": { fontSize: 1 },
  "../components/explorer/ColumnPicker.tsx": { rawButton: 2 },
  "../components/explorer/EventDetailPanel.tsx": { fontSize: 1, rawButton: 12 },
  "../components/explorer/EventGrid.tsx": { fontSize: 1, rawButton: 4 },
  "../components/explorer/ExportDialog.tsx": { rawButton: 1 },
  "../components/explorer/FilterChips.tsx": { rawButton: 1 },
  "../components/explorer/FilterRail.tsx": { rawButton: 8 },
  "../components/explorer/RoutineCollapseStat.tsx": { rawButton: 1 },
  "../components/explorer/TagFacetPanel.tsx": { rawButton: 1 },
  "../components/explorer/TimelineHistogram.tsx": { fontSize: 2, rawButton: 3 },
  "../components/layout/Footer.tsx": { fontSize: 1 },
  "../components/layout/TopBar.tsx": { rawButton: 3 },
  "../components/sources/ParserDownloadsPanel.tsx": { fontSize: 5 },
  "../components/stories/AddToStoryButton.tsx": { fontSize: 2, rawButton: 1 },
  "../components/stories/BlockFrame.tsx": { fontSize: 1, rawButton: 1 },
  "../components/stories/BlockPicker.tsx": { rawButton: 2 },
  "../components/stories/EmbedCards.tsx": { fontSize: 5 },
  "../components/stories/ExportsTab.tsx": { fontSize: 1, rawButton: 2 },
  "../components/stories/MarkdownBlock.tsx": { fontSize: 3, rawButton: 1 },
  "../components/stories/SnapshotRenderer.tsx": { fontSize: 5 },
  "../components/stories/StoryEditor.tsx": { fontSize: 1 },
  "../components/timelines/CreateTimelineDialog.tsx": { fontSize: 1 },
  "../components/timelines/EmbedWizard.tsx": { fontSize: 4, rawButton: 2 },
  "../components/timelines/FieldMappingEditor.tsx": { fontSize: 1, rawButton: 1 },
  "../components/tour/TourOverlay.tsx": { rawButton: 1 },
  "../components/ui/DateTimeField.tsx": { fontSize: 4 },
  "../components/ui/ErrorBoundary.tsx": { fontSize: 2 },
  "../components/ui/ProgressMeter.tsx": { fontSize: 1 },
  "../components/viz/ChartActionPopover.tsx": { rawButton: 2 },
  "../components/viz/FieldHistogramModal.tsx": { rawButton: 4 },
  "../components/viz/InheritedFiltersBar.tsx": { rawButton: 2 },
  "../components/viz/SavedChartsRail.tsx": { rawButton: 2 },
  "../components/viz/ScatterStatsPanel.tsx": { fontSize: 1 },
  "../components/viz/primitives/ExplainerPopover.tsx": { fontSize: 1, rawButton: 1 },
  "../components/viz/primitives/Legend.tsx": { rawButton: 1 },
  "../pages/ExplorerPage.tsx": { rawButton: 3 },
  "../pages/StoryEditorPage.tsx": { rawButton: 1 },
  "../pages/VisualizePage.tsx": { rawButton: 2 },
  "../pages/admin/AdminSettingsPage.tsx": { fontSize: 1 },
};
