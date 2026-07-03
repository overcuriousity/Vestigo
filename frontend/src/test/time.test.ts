import { describe, it, expect } from "vitest";
import {
  datetimeLocalToUtcIso,
  fmtTimestamp,
  isoToDatetimeLocalUtc,
} from "@/lib/time";

// UTC is the application-wide standard (issue #9): every one of these
// assertions must hold regardless of the machine's local timezone.

describe("fmtTimestamp", () => {
  it("renders in UTC, not browser-local time", () => {
    expect(fmtTimestamp("2024-01-01T10:00:00+02:00")).toBe("2024-01-01 08:00:00");
    expect(fmtTimestamp("2024-06-15T23:30:00Z")).toBe("2024-06-15 23:30:00");
  });

  it("passes through unparseable values and placeholders", () => {
    expect(fmtTimestamp(null)).toBe("—");
    expect(fmtTimestamp("not-a-date")).toBe("not-a-date");
  });
});

describe("datetime-local <-> UTC ISO round trip", () => {
  it("treats the typed wall-clock value as UTC", () => {
    expect(datetimeLocalToUtcIso("2024-01-01T10:00")).toBe("2024-01-01T10:00:00.000Z");
  });

  it("renders a UTC ISO string back as the same widget value (no drift)", () => {
    expect(isoToDatetimeLocalUtc("2024-01-01T10:00:00.000Z")).toBe("2024-01-01T10:00");
    // The full round trip is a fixed point — this is exactly the bug where
    // local-time parsing shifted the value by the UTC offset on every edit.
    const widget = "2024-03-31T02:30"; // DST-transition hour in Europe — extra hostile
    expect(isoToDatetimeLocalUtc(datetimeLocalToUtcIso(widget)!)).toBe(widget);
  });

  it("normalizes a non-UTC offset into its UTC wall clock", () => {
    expect(isoToDatetimeLocalUtc("2024-01-01T10:00:00+02:00")).toBe("2024-01-01T08:00");
  });

  it("handles empty and invalid input", () => {
    expect(datetimeLocalToUtcIso("")).toBeUndefined();
    expect(datetimeLocalToUtcIso("garbage")).toBeUndefined();
    expect(isoToDatetimeLocalUtc("")).toBe("");
    expect(isoToDatetimeLocalUtc(undefined)).toBe("");
    expect(isoToDatetimeLocalUtc("garbage")).toBe("");
  });
});
