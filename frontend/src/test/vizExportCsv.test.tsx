import { beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ExportControls } from "@/components/viz/ExportControls";
import * as exportLib from "@/components/viz/lib/export";
import { installRadixJsdomStubs } from "./helpers/radix";

vi.mock("@/components/viz/lib/export", async () => {
  const actual = await vi.importActual<typeof import("@/components/viz/lib/export")>(
    "@/components/viz/lib/export",
  );
  return { ...actual, downloadCsv: vi.fn() };
});

beforeAll(() => installRadixJsdomStubs());

describe("ExportControls with CSV", () => {
  it("offers CSV only when text is supplied, and downloads it verbatim", async () => {
    const { unmount } = render(<ExportControls svgRef={{ current: null }} filename="users_table" />);
    fireEvent.click(screen.getByRole("combobox", { name: "Export format" }));
    expect(await screen.findByRole("option", { name: "PNG" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "CSV" })).toBeNull();
    // The open listbox aria-hides everything outside its portal; start over
    // rather than query a trigger the accessibility tree no longer shows.
    unmount();
    render(
      <ExportControls
        svgRef={{ current: null }}
        filename="users_table"
        csv={"value,count\na,1\n"}
      />,
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Export format" }));
    fireEvent.click(await screen.findByRole("option", { name: "CSV" }));
    fireEvent.click(screen.getByRole("button", { name: /Export/ }));
    expect(exportLib.downloadCsv).toHaveBeenCalledWith("value,count\na,1\n", "users_table");
  });
});
