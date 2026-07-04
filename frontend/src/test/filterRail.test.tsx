/**
 * FilterRail search-mode control and field autocomplete behavior:
 * keyword is the default mode, semantic is an explicit opt-in gated on
 * embeddings existing, and the regex toggle only applies to keyword search.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FilterRail } from "@/components/explorer/FilterRail";
import { TooltipProvider } from "@/components/ui/Tooltip";
import type { EventFilters } from "@/api/types";

function renderRail(
  filters: EventFilters = {},
  props: Partial<React.ComponentProps<typeof FilterRail>> = {},
) {
  const onChange = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider><FilterRail
        filters={filters}
        onChange={onChange}
        views={[]}
        onApplyView={() => {}}
        onSaveView={() => {}}
        onSearchSubmit={() => {}}
        caseId="c1"
        timelineId="t1"
        {...props}
      /></TooltipProvider>
    </QueryClientProvider>,
  );
  return { onChange };
}

describe("FilterRail search mode control", () => {
  it("defaults to keyword mode with the regex toggle visible", () => {
    renderRail();
    expect(screen.getByText("Keyword")).toBeInTheDocument();
    expect(screen.getByText("Semantic")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /regular expression/i }),
    ).toBeInTheDocument();
  });

  it("disables the Semantic segment when the timeline has no embeddings", () => {
    renderRail({}, { hasVectors: false });
    expect(screen.getByText("Semantic").closest("button")).toBeDisabled();
  });

  it("switches to semantic mode and drops the regex flag", () => {
    const { onChange } = renderRail({ q: "^x", qRegex: true }, { hasVectors: true });
    fireEvent.click(screen.getByText("Semantic"));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ q: "^x", qMode: "semantic" }),
    );
    expect(onChange.mock.calls[0][0].qRegex).toBeUndefined();
  });

  it("hides the regex toggle in semantic mode", () => {
    renderRail({ qMode: "semantic" }, { hasVectors: true });
    expect(
      screen.queryByRole("button", { name: /regular expression/i }),
    ).not.toBeInTheDocument();
  });

  it("toggles the regex flag on the filters", () => {
    const { onChange } = renderRail({ q: "login" });
    fireEvent.click(screen.getByRole("button", { name: /regular expression/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ q: "login", qRegex: true }),
    );
  });

  it("shows an inline hint for an invalid regex pattern", () => {
    renderRail({ qRegex: true });
    const input = screen.getByPlaceholderText(/RE2 pattern/i);
    fireEvent.change(input, { target: { value: "([" } });
    expect(screen.getByText(/invalid regular expression/i)).toBeInTheDocument();
  });
});

describe("FilterRail field match modes", () => {
  it("switches the value placeholder when a match mode is picked", () => {
    renderRail();
    // First `*` segment button belongs to the Field=Value row.
    fireEvent.click(screen.getAllByRole("button", { name: "*" })[0]);
    expect(screen.getByPlaceholderText("e.g. 10.0.*")).toBeInTheDocument();
  });

  it("adds a wildcard filter with its mode in filterModes", () => {
    const { onChange } = renderRail();
    fireEvent.click(screen.getAllByRole("button", { name: "*" })[0]);
    const keyInputs = screen.getAllByPlaceholderText("field");
    fireEvent.change(keyInputs[0], { target: { value: "src_ip" } });
    fireEvent.change(screen.getByPlaceholderText("e.g. 10.0.*"), {
      target: { value: "10.0.*" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText("e.g. 10.0.*"), { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        filters: { src_ip: "10.0.*" },
        filterModes: { src_ip: "wildcard" },
      }),
    );
  });

  it("adds an exact filter without a filterModes entry", () => {
    const { onChange } = renderRail();
    const keyInputs = screen.getAllByPlaceholderText("field");
    fireEvent.change(keyInputs[0], { target: { value: "src_ip" } });
    const valInput = screen.getAllByPlaceholderText("value")[0];
    fireEvent.change(valInput, { target: { value: "10.0.1.1" } });
    fireEvent.keyDown(valInput, { key: "Enter" });
    const arg = onChange.mock.calls[0][0];
    expect(arg.filters).toEqual({ src_ip: "10.0.1.1" });
    expect(arg.filterModes).toBeUndefined();
  });

  it("shows the literal-glob trap hint when an exact value contains *", () => {
    renderRail();
    const valInput = screen.getAllByPlaceholderText("value")[0];
    fireEvent.change(valInput, { target: { value: "10.0.*" } });
    expect(screen.getByText(/matched literally in Exact mode/i)).toBeInTheDocument();
  });

  it("shows a regex hint for an invalid field pattern", () => {
    renderRail();
    // First `.*` segment button = Field=Value row's regex mode.
    fireEvent.click(screen.getAllByRole("button", { name: ".*" })[0]);
    const valInput = screen.getAllByPlaceholderText(/RE2 pattern/i)[0];
    fireEvent.change(valInput, { target: { value: "([" } });
    expect(screen.getByText(/invalid regular expression/i)).toBeInTheDocument();
  });
});

describe("FilterRail field autocomplete", () => {
  it("suggests timeline field names in the Field=Value key input", () => {
    renderRail({}, { fieldSuggestions: ["message", "status_code", "username"] });
    const keyInputs = screen.getAllByPlaceholderText("field");
    fireEvent.change(keyInputs[0], { target: { value: "status" } });
    expect(screen.getByText("status_code")).toBeInTheDocument();
  });

  it("suggests field names in the Field≠Value key input too", () => {
    renderRail({}, { fieldSuggestions: ["username"] });
    const keyInputs = screen.getAllByPlaceholderText("field");
    fireEvent.change(keyInputs[1], { target: { value: "user" } });
    expect(screen.getByText("username")).toBeInTheDocument();
  });
});
