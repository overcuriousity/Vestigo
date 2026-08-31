/**
 * ScenarioModal — bind a scenario's roles to this timeline's own fields.
 *
 * The modal is the whole of a scenario's domain knowledge made visible: it
 * names each role in words, shows which field it guessed and why the scenario
 * wants one there, and shows the filter it suggests as a row the analyst can
 * read and uncheck. Confirming is the rail's own two calls — a config patch
 * and, optionally, a filter merge — so a scenario can produce nothing an
 * analyst could not have built by hand from the rail.
 *
 * A required role the timeline cannot fill disables the confirm and is named
 * on the page; the scenario is never withdrawn from the gallery over it.
 */
import { useMemo, useState } from "react";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { FieldCombo, type FieldComboOption } from "@/components/ui/FieldCombo";
import {
  buildScenarioConfig,
  missingRoles,
  scenarioFilters,
  suggestBindings,
  type RoleBinding,
  type Scenario,
} from "@/components/viz/lib/scenarios";
import type { ChartConfig } from "@/components/viz/lib/chartConfig";
import type { EventFilters, VizFieldInfo } from "@/api/types";

interface Props {
  scenario: Scenario;
  fields: VizFieldInfo[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The chart patch, and the filter to merge — null when the scenario
   * suggests none or the analyst dropped it. */
  onApply: (patch: Partial<ChartConfig>, filters: EventFilters | null) => void;
}

function comboOption(f: VizFieldInfo): FieldComboOption {
  const hint =
    f.distinct == null
      ? "time field"
      : `${f.distinct.toLocaleString()} values${
          f.coverage == null ? "" : ` · ${Math.round(f.coverage * 100)}% coverage`
        }`;
  return { value: f.token, label: f.token, hint };
}

export function ScenarioModal({ scenario, fields, open, onOpenChange, onApply }: Props) {
  // Seeded once per opening: re-suggesting under the analyst would undo the
  // binding they just typed the moment the field list refetched.
  const [bindings, setBindings] = useState<RoleBinding>(() =>
    suggestBindings(scenario, fields),
  );
  const [useFilter, setUseFilter] = useState(true);

  const options = useMemo(() => fields.map(comboOption), [fields]);
  const unbound = missingRoles(scenario, bindings);
  const filters = scenarioFilters(scenario, bindings);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={scenario.label} description={scenario.question}>
        <div className="space-y-4">
          {scenario.roles.length === 0 && (
            <p className="text-sm text-[var(--color-fg-muted)]">
              This scenario needs no field — it charts every event in the current
              filters by weekday and hour.
            </p>
          )}
          {scenario.roles.map((role) => (
            <div key={role.key}>
              <label
                className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]"
                htmlFor={`scenario-role-${role.key}`}
              >
                {role.label}
                {!role.required && (
                  <span className="ml-1 normal-case text-[var(--color-fg-muted)]">
                    (optional)
                  </span>
                )}
              </label>
              <FieldCombo
                aria-label={role.label}
                data-testid={`scenario-role-${role.key}`}
                options={options}
                value={bindings[role.key] ?? ""}
                onChange={(v) =>
                  setBindings((b) => ({ ...b, [role.key]: v || undefined }))
                }
                placeholder="Choose a field…"
                size="sm"
              />
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">{role.hint}</p>
            </div>
          ))}

          {scenario.filter && (
            <div className="rounded border border-[var(--color-border)] p-3">
              <div className="flex items-start gap-2">
                <Checkbox
                  data-testid="scenario-filter-toggle"
                  checked={useFilter}
                  onCheckedChange={(v) => setUseFilter(v === true)}
                  id="scenario-filter"
                />
                <div>
                  <label
                    htmlFor="scenario-filter"
                    className="text-sm text-[var(--color-fg-primary)]"
                  >
                    {scenario.filter.label}
                  </label>
                  <p className="mt-0.5 text-xs text-[var(--color-fg-muted)]">
                    {scenario.filter.describe}. It is added to this page's filters,
                    so it shows in the filter bar and in the figure's caption — and
                    you can remove it there.
                  </p>
                </div>
              </div>
            </div>
          )}

          {unbound.length > 0 && (
            <p
              data-testid="scenario-unbound"
              className="text-xs text-[var(--color-fg-muted)]"
            >
              Bind {unbound.map((r) => r.label).join(" and ")} to a field in this
              timeline to render this scenario. Nothing here matched
              automatically — the field may be named differently, or this timeline
              may not carry it.
            </p>
          )}

          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              disabled={unbound.length > 0}
              onClick={() => {
                if (unbound.length > 0) return;
                onApply(
                  buildScenarioConfig(scenario, bindings),
                  useFilter ? filters : null,
                );
                onOpenChange(false);
              }}
            >
              Render figure
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
