import { describe, it, expect, vi, beforeEach } from "vitest";

const fetchBlobMock = vi.fn();
const triggerDownloadMock = vi.fn();

vi.mock("@/api/client", () => ({
  fetchBlob: (...a: unknown[]) => fetchBlobMock(...a),
}));

vi.mock("@/lib/download", () => ({
  triggerDownload: (...a: unknown[]) => triggerDownloadMock(...a),
}));

import { downloadFieldInventory } from "@/api/export";
import type { EventFilters } from "@/api/types";

beforeEach(() => {
  fetchBlobMock.mockReset();
  triggerDownloadMock.mockReset();
  fetchBlobMock.mockResolvedValue(new Blob(["value\n"]));
});

/**
 * comma, semicolon and pipe all wrote `…-inventory.csv`, so exporting one
 * field twice with different separators saved the second as `…(1).csv` while
 * the analyst reopened the first — indistinguishable from the separator
 * picker doing nothing, which is exactly how it was reported.
 *
 * This name must also match the server's own Content-Disposition
 * (`export_field_inventory`), since either can be the one the browser uses.
 */
describe("value-inventory download filename", () => {
  it.each([
    ["comma", "c1-t1-attr_src_ip-inventory-comma.csv"],
    ["semicolon", "c1-t1-attr_src_ip-inventory-semicolon.csv"],
    ["pipe", "c1-t1-attr_src_ip-inventory-pipe.csv"],
    ["tab", "c1-t1-attr_src_ip-inventory-tab.tsv"],
  ])("names the %s separator", async (separator, expected) => {
    await downloadFieldInventory(
      "c1",
      "t1",
      {} as EventFilters,
      {
        fields: ["attr:src_ip"],
        columns: ["value"],
        separator: separator as "comma" | "semicolon" | "pipe" | "tab",
        orderBy: "value_asc",
      },
    );

    expect(triggerDownloadMock).toHaveBeenCalledWith(expect.any(Blob), expected);
  });

  it("distinguishes two separators of the same file extension", async () => {
    const name = async (separator: "comma" | "semicolon") => {
      triggerDownloadMock.mockReset();
      await downloadFieldInventory(
        "c1",
        "t1",
        {} as EventFilters,
        { fields: ["attr:src_ip"], columns: ["value"], separator, orderBy: "value_asc" },
      );
      return triggerDownloadMock.mock.calls[0][1];
    };

    expect(await name("comma")).not.toBe(await name("semicolon"));
  });
});
