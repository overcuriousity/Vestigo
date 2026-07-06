/**
 * Spotlight overlay for the onboarding tour.
 *
 * Dims the page with a huge box-shadow around the current step's target and
 * places an explaining card beside it. The spotlight itself is
 * `pointer-events: none` so the highlighted control stays fully clickable —
 * most steps advance by the user actually performing the action.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { matchPath, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { TOUR_STEPS } from "@/lib/tourSteps";
import { useTourStore } from "@/stores/tour";

const SPOT_PAD = 6;
const CARD_GAP = 12;
const VIEWPORT_MARGIN = 12;

function rectsEqual(a: DOMRect | null, b: DOMRect | null): boolean {
  if (!a || !b) return a === b;
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height;
}

export function TourOverlay() {
  const stepIndex = useTourStore((s) => s.stepIndex);
  const next = useTourStore((s) => s.next);
  const back = useTourStore((s) => s.back);
  const skip = useTourStore((s) => s.skip);
  const location = useLocation();

  const step = TOUR_STEPS[stepIndex];
  const onRoute = step ? !!matchPath(step.routePattern, location.pathname) : false;

  const [target, setTarget] = useState<Element | null>(null);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [triggerOpen, setTriggerOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const [cardPos, setCardPos] = useState<{ left: number; top: number } | null>(null);
  // The waiting hint only appears once the target has stayed unresolved for a
  // moment — targets normally resolve within one 200ms poll on step entry,
  // and flashing the hint for that instant reads like an error.
  const [showWaitHint, setShowWaitHint] = useState(false);
  useEffect(() => {
    if (rect) {
      setShowWaitHint(false);
      return;
    }
    const t = setTimeout(() => setShowWaitHint(true), 800);
    return () => clearTimeout(t);
  }, [rect, step]);

  // Resolve + track the target element. A 200ms poll (only while the tour is
  // active) handles all the hard cases uniformly: target not yet rendered
  // (query loading, dialog closed), target unmounting, and position changes
  // from scrolling/resizing/virtualization. Scroll/resize listeners make
  // position updates immediate between polls.
  useEffect(() => {
    if (!step?.selector || !onRoute) {
      setTarget(null);
      setRect(null);
      return;
    }
    let raf = 0;
    const selectors = Array.isArray(step.selector) ? step.selector : [step.selector];
    const update = () => {
      let el: Element | null = null;
      for (const sel of selectors) {
        el = document.querySelector(sel);
        if (el) break;
      }
      setTarget((prev) => (prev === el ? prev : el));
      setRect((prev) => {
        const r = el ? el.getBoundingClientRect() : null;
        return rectsEqual(prev, r) ? prev : r;
      });
      setTriggerOpen(!!el && el.getAttribute("data-state") === "open");
    };
    const scheduled = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(update);
    };
    update();
    const iv = setInterval(update, 200);
    window.addEventListener("scroll", scheduled, true);
    window.addEventListener("resize", scheduled, true);
    return () => {
      clearInterval(iv);
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", scheduled, true);
      window.removeEventListener("resize", scheduled, true);
    };
  }, [step, onRoute]);

  // Force hover-only controls visible while spotlighted (e.g. the filter
  // in/out buttons in the event detail panel are opacity-0 until row hover).
  useEffect(() => {
    if (!step?.forceVisibleClass || !target) return;
    target.classList.add("tour-force-visible");
    return () => target.classList.remove("tour-force-visible");
  }, [step, target]);

  // Place the card beside the spotlight, flipping/clamping to the viewport.
  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    if (!rect) {
      setCardPos(null);
      return;
    }
    const c = card.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let side = step.side;
    const fits = {
      top: rect.top - SPOT_PAD - CARD_GAP - c.height >= VIEWPORT_MARGIN,
      bottom: rect.bottom + SPOT_PAD + CARD_GAP + c.height <= vh - VIEWPORT_MARGIN,
      left: rect.left - SPOT_PAD - CARD_GAP - c.width >= VIEWPORT_MARGIN,
      right: rect.right + SPOT_PAD + CARD_GAP + c.width <= vw - VIEWPORT_MARGIN,
    };
    if (!fits[side]) {
      const flip = { top: "bottom", bottom: "top", left: "right", right: "left" } as const;
      if (fits[flip[side]]) side = flip[side];
    }
    let left: number;
    let top: number;
    if (side === "top" || side === "bottom") {
      left = rect.left + rect.width / 2 - c.width / 2;
      top =
        side === "top"
          ? rect.top - SPOT_PAD - CARD_GAP - c.height
          : rect.bottom + SPOT_PAD + CARD_GAP;
    } else {
      top = rect.top + rect.height / 2 - c.height / 2;
      left =
        side === "left"
          ? rect.left - SPOT_PAD - CARD_GAP - c.width
          : rect.right + SPOT_PAD + CARD_GAP;
    }
    left = Math.min(Math.max(left, VIEWPORT_MARGIN), vw - c.width - VIEWPORT_MARGIN);
    top = Math.min(Math.max(top, VIEWPORT_MARGIN), vh - c.height - VIEWPORT_MARGIN);
    setCardPos((prev) => (prev && prev.left === left && prev.top === top ? prev : { left, top }));
  }, [rect, step]);

  if (!step) return null;
  // The user is inside the dialog this step told them to open — get out of
  // the way until it closes (cancel) or the step advances (success).
  if (step.hideWhileTriggerOpen && triggerOpen) return null;

  const z = step.aboveDialog ? 70 : 60;
  const isLast = stepIndex === TOUR_STEPS.length - 1;
  const showNext = step.advance.type === "next" || step.alsoNext;
  // Back only where it can't strand the user: previous step must be on the
  // same page, so Back never leaves the user on a target that no longer
  // exists. Event-advanced steps are fine to go back to as well — their
  // triggering action (click a button, open a dialog) is always safe to
  // repeat and re-fires the same event, letting the tour move forward again.
  const prev = TOUR_STEPS[stepIndex - 1];
  const showBack = !!prev && prev.routePattern === step.routePattern;
  const spotlight = onRoute && rect;

  const card = (
    <div
      ref={cardRef}
      role="dialog"
      aria-label={`Tour step ${stepIndex + 1} of ${TOUR_STEPS.length}: ${step.title}`}
      className="w-80 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] p-4 shadow-lg"
      // pointerEvents must be explicit: an open Radix modal Dialog sets
      // `pointer-events: none` on <body>, which this body-portaled card would
      // otherwise inherit — making Next/Skip unclickable on above-dialog steps.
      // stopPropagation keeps Radix's outside-pointerdown detection from
      // dismissing that dialog when the user clicks the card.
      onPointerDown={(e) => e.stopPropagation()}
      style={
        cardPos
          ? {
              position: "fixed",
              left: cardPos.left,
              top: cardPos.top,
              zIndex: z + 1,
              pointerEvents: "auto",
            }
          : {
              position: "fixed",
              left: "50%",
              top: "50%",
              transform: "translate(-50%, -50%)",
              zIndex: z + 1,
              pointerEvents: "auto",
            }
      }
    >
      <div className="mb-1 text-xs text-[var(--color-fg-muted)]">
        {stepIndex + 1} / {TOUR_STEPS.length}
      </div>
      <h3 className="mb-1.5 text-sm font-semibold text-[var(--color-fg-primary)]">{step.title}</h3>
      <p className="text-sm text-[var(--color-fg-secondary)]">{step.body}</p>
      {!spotlight && step.selector && showWaitHint && (
        <p className="mt-2 text-xs text-[var(--color-warning)]">
          {onRoute
            ? "Waiting for this part of the page to appear…"
            : "This step happens on a different page — navigate back to continue, or skip the tour."}
        </p>
      )}
      <div className="mt-3 flex items-center justify-between gap-2">
        <button
          onClick={skip}
          className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
        >
          Skip tour
        </button>
        <div className="flex items-center gap-2">
          {showBack && (
            <Button variant="ghost" size="sm" onClick={back}>
              Back
            </Button>
          )}
          {showNext && (
            <Button variant="accent" size="sm" onClick={next}>
              {isLast ? "Finish" : "Next"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );

  return createPortal(
    <>
      {spotlight ? (
        <div
          style={{
            position: "fixed",
            left: rect.left - SPOT_PAD,
            top: rect.top - SPOT_PAD,
            width: rect.width + SPOT_PAD * 2,
            height: rect.height + SPOT_PAD * 2,
            borderRadius: 8,
            boxShadow: "0 0 0 2px var(--color-accent), 0 0 0 9999px rgba(0, 0, 0, 0.55)",
            pointerEvents: "none",
            zIndex: z,
          }}
        />
      ) : (
        // No resolvable target: light non-blocking dim so the user can still
        // navigate/perform whatever the step is waiting for.
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0, 0, 0, 0.3)",
            pointerEvents: "none",
            zIndex: z,
          }}
        />
      )}
      {card}
    </>,
    document.body,
  );
}
