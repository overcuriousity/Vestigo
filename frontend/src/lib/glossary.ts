/**
 * Single source of truth for the short definitions of the investigation
 * vocabulary. Shared by the inline term tooltips (FrameBar, WindowsNormality,
 * detector headers) and the first-run guidance explainer so the copy never
 * drifts between them. Keep each entry to one or two plain sentences — these
 * render inside the string-content `ui/Tooltip`.
 */
export const GLOSSARY = {
  scanAllEvents:
    "Score every event against the whole corpus (self-baseline). No reference window needed — good for a first pass over unfamiliar data.",
  compareBaseline:
    "Score one or more suspect windows against a baseline you trust as normal. Surfaces what changed relative to known-good activity.",
  baseline:
    "A time window you declare as normal / known-good. Detectors learn what's expected here, then flag deviations in the suspect windows.",
  suspectWindow:
    "A time window you want to investigate, scored against the baseline. Add several to compare different periods against the same normal.",
  selfBaseline:
    "The detector learns 'normal' from all scanned events themselves — there is no separate reference window.",
  temporal:
    "The detector learns 'normal' from a baseline window, then flags deviations in each suspect window scored against it. Suspect windows need not be adjacent to the baseline.",
  normalValues:
    "Your verdicts on findings. Normal = expected behavior, extends the baseline and suppresses detection. Dismissed = noise, hidden from view only. Confirmed = escalated, survives re-scans.",
} as const;

export type GlossaryKey = keyof typeof GLOSSARY;
