/**
 * FieldCombo — the single field-selection control shared by every surface that
 * asks "which field?" (Visualize's axes, the method knobs, log templates,
 * sequence patterns, export, the compare filter editor).
 *
 * It replaces six separate `<select>`/Radix `Select` pickers, so the contract
 * these tests pin is the one all six depend on: browse the whole list without
 * typing, filter by what you can see (token *or* label), commit a token that
 * the inventory never reported, and never hand the caller a label where it
 * expects a token.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FieldCombo } from "@/components/ui/FieldCombo";

const OPTIONS = [
  { value: "src_ip", label: "Source IP", hint: "1,204 distinct" },
  { value: "src_port", label: "Source port" },
  { value: "attr:user_agent", label: "user_agent" },
];

describe("FieldCombo", () => {
  it("offers the whole list on focus, before anything is typed", () => {
    render(<FieldCombo options={OPTIONS} value="" onChange={() => {}} />);

    fireEvent.focus(screen.getByRole("combobox"));

    expect(screen.getByRole("option", { name: /Source IP/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Source port/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /user_agent/ })).toBeInTheDocument();
  });

  it("emits the option's token, not the label the row displays", () => {
    const onChange = vi.fn();
    render(<FieldCombo options={OPTIONS} value="" onChange={onChange} />);

    const input = screen.getByRole("combobox");
    fireEvent.focus(input);
    // The row commits on mousedown, not click, so focus never leaves the
    // input and the blur handler cannot drop the selection first.
    fireEvent.mouseDown(screen.getByRole("option", { name: /Source IP/ }));

    expect(onChange).toHaveBeenCalledWith("src_ip");
  });

  it("filters on the label, not only the token", () => {
    render(<FieldCombo options={OPTIONS} value="" onChange={() => {}} />);

    // "port" appears in `src_port`'s label and its token; "Source IP" is only
    // reachable through the label, which is the case that used to be lost when
    // these were native selects filtering on nothing at all.
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "source i" } });

    expect(screen.getByRole("option", { name: /Source IP/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Source port/ })).not.toBeInTheDocument();
  });

  it("filters on the token when the label would not match", () => {
    render(<FieldCombo options={OPTIONS} value="" onChange={() => {}} />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "attr:" } });

    expect(screen.getByRole("option", { name: /user_agent/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Source IP/ })).not.toBeInTheDocument();
  });

  it("commits a token the inventory never reported", () => {
    const onChange = vi.fn();
    render(<FieldCombo options={OPTIONS} value="" onChange={onChange} />);

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "attr:not_in_inventory" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("attr:not_in_inventory");
  });

  it("refuses free text when the caller turned it off", () => {
    const onChange = vi.fn();
    render(
      <FieldCombo options={OPTIONS} value="" onChange={onChange} allowFreeText={false} />,
    );

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "nonsense" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).not.toHaveBeenCalled();
  });

  it("selects the matching option when a token is typed in full", () => {
    const onChange = vi.fn();
    render(<FieldCombo options={OPTIONS} value="" onChange={onChange} />);

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "src_ip" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // Not "free text that happens to look like src_ip" — the same commit the
    // list row makes, so no caller has to dedupe the two paths.
    expect(onChange).toHaveBeenCalledWith("src_ip");
  });

  it("takes the keyboard highlight over the typed text", () => {
    const onChange = vi.fn();
    render(<FieldCombo options={OPTIONS} value="" onChange={onChange} />);

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "src" } });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("src_port");
  });

  it("reverts to the committed value on Escape without telling the caller", () => {
    const onChange = vi.fn();
    render(<FieldCombo options={OPTIONS} value="src_ip" onChange={onChange} />);

    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("src_ip");

    fireEvent.change(input, { target: { value: "half-typed" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(input.value).toBe("src_ip");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows the raw token in the box, not the label", () => {
    render(<FieldCombo options={OPTIONS} value="attr:user_agent" onChange={() => {}} />);

    // The token is what an analyst would type and what every caller stores,
    // so displaying "user_agent" here would make the box unusable as a source
    // of the thing you are about to copy into a filter.
    expect((screen.getByRole("combobox") as HTMLInputElement).value).toBe("attr:user_agent");
  });

  it("groups rows under the caller's section headers", () => {
    render(
      <FieldCombo
        options={[
          { value: "message", label: "Message", group: "Standard" },
          { value: "attr:user_agent", label: "user_agent", group: "Dynamic fields" },
        ]}
        value=""
        onChange={() => {}}
      />,
    );

    fireEvent.focus(screen.getByRole("combobox"));

    expect(screen.getByText("Standard")).toBeInTheDocument();
    expect(screen.getByText("Dynamic fields")).toBeInTheDocument();
  });

  it("says so when the committed value is not a field this timeline reported", () => {
    render(<FieldCombo options={OPTIONS} value="attr:typoo" onChange={() => {}} />);

    // Free entry means a typo commits silently and the detector or chart comes
    // back empty with nothing naming the cause. The note is the only thing
    // standing between that and a wasted scan.
    expect(screen.getByText(/not in this timeline/i)).toBeInTheDocument();
  });

  it("stays quiet about an empty value", () => {
    render(<FieldCombo options={OPTIONS} value="" onChange={() => {}} />);

    expect(screen.queryByText(/not in this timeline/i)).not.toBeInTheDocument();
  });

  it("stays quiet while the field list is still loading", () => {
    // Every caller feeds this from a query, so `options` is empty for as long
    // as the fetch takes. A field named in the URL is not suspect because a
    // request has not landed — and the note claimed exactly that on every
    // Visualize load carrying `c_field`.
    render(<FieldCombo options={[]} value="src_ip" onChange={() => {}} />);

    expect(screen.queryByText(/not in this timeline/i)).not.toBeInTheDocument();
  });

  it("keeps Escape to itself instead of closing the surface around it", () => {
    // The sheet and the export dialog both close on Escape — the sheet from a
    // `window` listener, the dialog from a capture-phase `document` one. A
    // combo that let the key through reverted its draft *and* threw away every
    // knob value beside it.
    const onChange = vi.fn();
    const outer = vi.fn();
    window.addEventListener("keydown", outer);
    try {
      render(<FieldCombo options={OPTIONS} value="src_ip" onChange={onChange} />);
      const input = screen.getByRole("combobox") as HTMLInputElement;
      fireEvent.change(input, { target: { value: "half-typed" } });
      fireEvent.keyDown(input, { key: "Escape" });

      expect(input.value).toBe("src_ip");
      expect(outer).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", outer);
    }
  });

  it("lets Escape past when the box is not the one holding it", () => {
    const outer = vi.fn();
    window.addEventListener("keydown", outer);
    try {
      render(<FieldCombo options={OPTIONS} value="src_ip" onChange={() => {}} />);
      fireEvent.focus(screen.getByRole("combobox"));
      fireEvent.keyDown(document.body, { key: "Escape" });

      expect(outer).toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", outer);
    }
  });

  it("does not commit an empty field to a caller that has no empty option", () => {
    // `logTemplates(..., { field: "" })` is a request no list here can express
    // and no caller guards — a state the `<select>`s this replaced could not
    // reach at all.
    const onChange = vi.fn();
    render(<FieldCombo options={OPTIONS} value="src_ip" onChange={onChange} />);

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).not.toHaveBeenCalled();
  });

  it("commits the empty field where the caller offers it", () => {
    // The method knobs do: `""` is the method's own default there, named after
    // what it then does.
    const onChange = vi.fn();
    render(
      <FieldCombo
        options={[{ value: "", label: "the whole scope" }, ...OPTIONS]}
        value="src_ip"
        onChange={onChange}
      />,
    );

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith("");
  });

  it("names the highlighted row for a screen reader", () => {
    render(<FieldCombo options={OPTIONS} value="" onChange={() => {}} />);

    const input = screen.getByRole("combobox");
    fireEvent.focus(input);
    expect(input).not.toHaveAttribute("aria-activedescendant");

    fireEvent.keyDown(input, { key: "ArrowDown" });

    const active = input.getAttribute("aria-activedescendant");
    expect(active).toBeTruthy();
    expect(document.getElementById(active!)).toHaveTextContent("Source IP");
  });

  it("keeps the portaled list clickable inside a modal dialog", () => {
    // A modal Radix layer sets `body { pointer-events: none }` and re-enables
    // it on its own node only, so a list portaled to `document.body` was
    // mouse-dead in the export dialog: every row click swallowed.
    render(<FieldCombo options={OPTIONS} value="" onChange={() => {}} />);
    fireEvent.focus(screen.getByRole("combobox"));

    expect(screen.getByRole("listbox").className).toContain("pointer-events-auto");
  });
});
