/**
 * Anchor a portaled dropdown to its trigger, in fixed viewport coordinates.
 *
 * A dropdown rendered inline is clipped by the first `overflow: hidden`
 * ancestor, and every surface that needs one here sits inside a scrolling rail
 * or an overlay sheet. Portaling to `document.body` escapes that, but then the
 * list no longer moves with its anchor — so the position is recomputed on
 * layout and on any scroll or resize while open, and the list flips above the
 * anchor when there is no room below.
 *
 * Extracted verbatim from `TagInput`, which had the only copy; `FieldCombo`
 * needs the same behaviour and a second copy would be a second thing to keep
 * in step.
 */
import { useLayoutEffect, useRef, useState } from "react";

/** Max dropdown height in px — must match the `max-h-48` (12rem) on the list. */
export const DROPDOWN_MAX_HEIGHT = 192;
const DROPDOWN_GAP = 2;

export interface AnchoredPosition {
  left: number;
  top: number;
  width: number;
}

export function useAnchoredDropdown({
  open,
  dropUp = false,
  /** Any value that changes the list's height, so the flip is recomputed. */
  itemCount,
}: {
  open: boolean;
  dropUp?: boolean;
  itemCount: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const [pos, setPos] = useState<AnchoredPosition | null>(null);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = containerRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const listHeight = Math.min(listRef.current?.scrollHeight ?? 0, DROPDOWN_MAX_HEIGHT);
      const roomBelow = window.innerHeight - anchor.bottom;
      const roomAbove = anchor.top;
      const placeUp = dropUp
        ? roomAbove >= listHeight || roomAbove > roomBelow
        : roomBelow < listHeight && roomAbove > roomBelow;
      const top = placeUp
        ? anchor.top - DROPDOWN_GAP - listHeight
        : anchor.bottom + DROPDOWN_GAP;
      setPos({ left: anchor.left, top, width: anchor.width });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, dropUp, itemCount]);

  return { containerRef, listRef, pos };
}
