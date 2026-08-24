import { createContext } from "react";

/**
 * Width `ChartFrame` draws at when nothing will ever measure its container.
 *
 * `renderToStaticMarkup` (the story export, `stories/exportHtml.ts`) runs no
 * effects and has no `ResizeObserver`, so `ChartFrame`'s measurement never
 * happens and its `width > 0` gate emits no `<svg>` at all — exports carried
 * their prose and silently dropped every chart (issue #197). A context lets
 * that one caller pin a width without every chart component having to thread
 * a prop down to the frame.
 *
 * `null` (the default) keeps the live behaviour exactly as it was: start at
 * zero, draw once measured. A supplied width is only the *starting* value —
 * a real `ResizeObserver` still overrides it, so a provider-wrapped tree
 * rendered into a live DOM stays responsive.
 *
 * Its own module rather than a `ChartFrame` export: a component file that
 * also exports a context loses fast refresh, and oxlint says so.
 */
export const ChartStaticWidthContext = createContext<number | null>(null);
