/**
 * resolveCollapseRoutine — issue #147.
 *
 * Muting a template writes a `kind="routine"` disposition, and the Templates /
 * Patterns copy promises its events leave the grid immediately. A mute *is* a
 * filter, so it must apply on creation: collapse is on by default whenever any
 * routine disposition exists, and the top-bar toggle is a temporary reveal
 * override rather than the opt-in switch it used to be.
 */
import { describe, expect, it } from "vitest";
import {
  resolveCollapseRoutine,
  routineSignature,
  type RoutineOverride,
} from "@/lib/routineCollapse";
import type { Disposition } from "@/api/types";

function disp(id: string, kind: string, detector = "log_template"): Disposition {
  return { id, kind, detector } as Disposition;
}

const NONE: RoutineOverride = null;

describe("routineSignature", () => {
  it("is empty when no routine disposition exists", () => {
    expect(routineSignature([])).toBe("");
    expect(routineSignature([disp("a", "dismissed"), disp("b", "confirmed")])).toBe("");
  });

  it("covers routine dispositions only, order-independently", () => {
    const a = routineSignature([disp("x", "routine"), disp("y", "routine"), disp("z", "normal")]);
    const b = routineSignature([disp("z", "normal"), disp("y", "routine"), disp("x", "routine")]);
    expect(a).toBe(b);
    expect(a).not.toBe("");
  });

  it("changes when a mute is added or removed", () => {
    const one = routineSignature([disp("x", "routine")]);
    const two = routineSignature([disp("x", "routine"), disp("y", "routine")]);
    expect(one).not.toBe(two);
  });
});

describe("resolveCollapseRoutine", () => {
  it("stays off while no routine disposition exists", () => {
    expect(resolveCollapseRoutine("", NONE)).toBe(false);
  });

  it("is on as soon as a mute exists — the filter applies on creation", () => {
    expect(resolveCollapseRoutine("sig-1", NONE)).toBe(true);
  });

  it("honours an explicit reveal against the current disposition set", () => {
    const override: RoutineOverride = { value: false, signature: "sig-1" };
    expect(resolveCollapseRoutine("sig-1", override)).toBe(false);
  });

  it("re-applies collapse when a new mute lands after a reveal", () => {
    // Analyst revealed routine events, then muted another template: the new
    // filter must take effect rather than inheriting the stale reveal.
    const override: RoutineOverride = { value: false, signature: "sig-1" };
    expect(resolveCollapseRoutine("sig-2", override)).toBe(true);
  });

  it("honours an explicit collapse from an agent apply", () => {
    const override: RoutineOverride = { value: true, signature: "sig-1" };
    expect(resolveCollapseRoutine("sig-1", override)).toBe(true);
  });

  it("never collapses on an empty scope, even under an explicit override", () => {
    // Unmuting the last template drops the signature to "": there is nothing
    // left to collapse, so the stat must not claim an active collapse.
    const override: RoutineOverride = { value: true, signature: "" };
    expect(resolveCollapseRoutine("", override)).toBe(false);
  });
});
