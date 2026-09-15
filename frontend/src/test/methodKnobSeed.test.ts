/**
 * seedFromParams — reopening a stored detector entry on what it configured.
 *
 * A `choice` knob's first option is its default, and the form spells the
 * default `""` so an untouched knob stays out of the params entirely. An
 * entry stored with that default written out explicitly (the API and the
 * agent both accept it) must still land on a value the `<select>` can show.
 */
import { describe, expect, it } from "vitest";
import { seedFromParams } from "@/components/analysis/MethodKnobForm";
import { METHODS } from "@/components/analysis/method-registry";

const entropy = METHODS.find((m) => m.id === "entropy")!;
const variant = entropy.knobs.find((k) => k.param === "variant")!;

describe("seedFromParams", () => {
  it("folds an explicitly stored default onto the empty choice", () => {
    // "shannon" is options[0] — the default — and the form renders that
    // option with value="". Seeding it verbatim would hand the <select> a
    // value no <option> carries, and the browser would silently display the
    // first option while the form's state said something else.
    expect(variant.options?.[0]?.value).toBe("shannon");
    const { values } = seedFromParams(entropy, { variant: "shannon" });
    expect(values.variant).toBe("");
  });

  it("keeps a non-default choice verbatim", () => {
    const { values } = seedFromParams(entropy, { variant: "bigram" });
    expect(values.variant).toBe("bigram");
  });

  it("leaves an absent knob unseeded", () => {
    const { values } = seedFromParams(entropy, {});
    expect("variant" in values).toBe(false);
  });

  it("every seeded choice value is one the select can render", () => {
    // The invariant behind the fix, checked across the whole registry rather
    // than the one method that happens to have a choice knob today.
    for (const method of METHODS) {
      for (const knob of method.knobs) {
        if (knob.kind !== "choice") continue;
        for (const option of knob.options ?? []) {
          const { values } = seedFromParams(method, { [knob.param]: option.value });
          const renderable = ["", ...(knob.options ?? []).slice(1).map((o) => o.value)];
          expect(renderable).toContain(values[knob.param]);
        }
      }
    }
  });
});
