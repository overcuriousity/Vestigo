/**
 * A popover opened inside a modal dialog must not dismiss the dialog.
 *
 * The detector wizard is a Dialog whose "Fields to scan" control is a
 * Popover, so every click inside that field list is a click outside the
 * dialog's own DOM (the popover portals to the body). Radix handles that by
 * registering nested dismissable layers — but only when every Radix package
 * shares *one* instance of `@radix-ui/react-dismissable-layer`. A duplicated
 * copy in the install tree gives Dialog and Popover separate layer
 * registries, the popover's layer never joins the dialog's, and the first
 * click on a field closed the whole wizard.
 *
 * So this is a packaging regression test as much as a UI one: it fails
 * whenever the dependency tree grows a second copy of that module again.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/Popover";

describe("popover inside a modal dialog", () => {
  it("survives a click on its own content", () => {
    render(
      <Dialog defaultOpen>
        <DialogContent title="Configure Value combos">
          <Popover>
            <PopoverTrigger>Fields</PopoverTrigger>
            <PopoverContent>
              <button>dst_ip</button>
            </PopoverContent>
          </Popover>
        </DialogContent>
      </Dialog>,
    );

    fireEvent.click(screen.getByText("Fields"));
    const field = screen.getByText("dst_ip");
    fireEvent.pointerDown(field);
    fireEvent.click(field);

    expect(screen.queryByText("Configure Value combos")).not.toBeNull();
    expect(screen.queryByText("dst_ip")).not.toBeNull();
  });
});
