import { describe, expect, it } from "vitest";
import {
  DEFAULT_TABLE_COLUMNS,
  effectiveColumns,
  tableCsv,
  tableRowModels,
} from "@/components/viz/lib/tableRows";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";
import { OTHER_KEY } from "@/components/viz/lib/colors";
import type { FieldTableResponse } from "@/api/types";

const data: FieldTableResponse = {
  kind: "table",
  field: "attr:user",
  second_field: "attr:host",
  total: 11,
  distinct: 4,
  rows: [
    {
      value: "alice",
      count: 5,
      share: 5 / 11,
      first_seen: "2026-07-20T01:00:00+00:00",
      last_seen: "2026-07-21T05:00:00+00:00",
      distinct_second: 2,
    },
    { value: "bob", count: 3, share: 3 / 11, first_seen: null, last_seen: null, distinct_second: 1 },
  ],
  remainder: { count: 3, share: 3 / 11, distinct_values: 2 },
  sort: { by: "count", dir: "desc" },
};
const config: ChartConfig = {
  ...DEFAULT_CHART_CONFIG,
  chartType: "table",
  field: "attr:user",
  fieldY: "attr:host",
};

describe("tableRows", () => {
  it("defaults the columns, appending distinct_second only when a second field is set", () => {
    expect(effectiveColumns({ ...config, fieldY: null })).toEqual(DEFAULT_TABLE_COLUMNS);
    expect(effectiveColumns(config)).toEqual([...DEFAULT_TABLE_COLUMNS, "distinct_second"]);
    expect(
      effectiveColumns({ ...config, inputs: { columns: ["count", "distinct_second"] } }),
    ).toEqual(["count", "distinct_second"]);
    expect(
      effectiveColumns({
        ...config,
        fieldY: null,
        inputs: { columns: ["count", "distinct_second"] },
      }),
    ).toEqual(["count"]);
  });

  it("models rows in server order, formats cells, and appends a remainder row when values were cut", () => {
    const rows = tableRowModels(data, config, ["bob"]);
    expect(rows.map((r) => r.key)).toEqual(["alice", "bob", OTHER_KEY]);
    expect(rows[0].cells).toEqual({
      count: "5",
      share: "45.5%",
      first_seen: "2026-07-20 01:00:00Z",
      last_seen: "2026-07-21 05:00:00Z",
      distinct_second: "2",
    });
    expect(rows[1].cells.first_seen).toBe("—");
    expect(rows[1].highlighted).toBe(true);
    expect(rows[2]).toMatchObject({
      isRemainder: true,
      label: "Remainder (2 more values)",
      count: 3,
    });
    expect(rows[2].cells.first_seen).toBe("—");
    expect(
      tableRowModels({ ...data, remainder: null }, config, []).map((r) => r.isRemainder),
    ).toEqual([false, false]);
  });

  it("writes CSV with caption comment lines, a header, raw values and the remainder", () => {
    const csv = tableCsv(data, { ...config, inputs: { columns: ["count", "share"] } }, [
      "Vestigo — case c",
      "field: attr:user",
    ]);
    expect(csv.split("\n")).toEqual([
      "# Vestigo — case c",
      "# field: attr:user",
      "value,count,share",
      "alice,5,0.45454545454545453",
      "bob,3,0.2727272727272727",
      "Remainder (2 more values),3,0.2727272727272727",
      "",
    ]);
  });

  it("quotes CSV cells that contain a comma, quote or newline", () => {
    const tricky = { ...data, rows: [{ ...data.rows[0], value: 'a,"b"' }], remainder: null };
    expect(
      tableCsv(tricky, { ...config, inputs: { columns: ["count"] } }, []).split("\n")[1],
    ).toBe('"a,""b""",5');
  });
});
