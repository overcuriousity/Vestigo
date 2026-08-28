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
});
