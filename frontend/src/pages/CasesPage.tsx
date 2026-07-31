import { CaseList } from "@/components/cases/CaseList";
import { CreateCaseDialog } from "@/components/cases/CreateCaseDialog";
import { ImportCaseDialog } from "@/components/cases/ImportCaseDialog";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { useCapabilities } from "@/api/health";
import { ShieldAlert } from "lucide-react";

export function CasesPage() {
  // Case transfer is hidden entirely when the operator disabled it
  // (VESTIGO_TRANSFER_ENABLED / the admin settings page).
  const { transfer } = useCapabilities();
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {/* Hero header */}
        <div className="mb-8 flex items-start gap-4">
          <ShieldAlert
            size={32}
            className="mt-0.5 shrink-0 text-[var(--color-accent)]"
          />
          <div className="flex-1">
            <h1 className="text-xl font-semibold text-[var(--color-fg-primary)]">
              Investigation Cases
            </h1>
            <p className="mt-1 text-sm text-[var(--color-fg-muted)]">
              Each case groups related timelines under a single investigation context.
              Create a case, add timelines, upload log files, and start exploring.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {transfer && <ImportCaseDialog />}
            <CreateCaseDialog />
          </div>
        </div>

        <CaseList />

        <div className="mt-8">
          <GuidancePanel id="cases-page" />
        </div>
      </div>
    </div>
  );
}
