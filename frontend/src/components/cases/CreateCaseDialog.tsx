import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { casesApi } from "@/api/cases";
import { adminApi } from "@/api/admin";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/Select";
import { Plus } from "lucide-react";
import { manageableTeams } from "@/lib/caseAccess";
import { useAuthStore } from "@/stores/auth";

const PERSONAL = "__personal__";

export function CreateCaseDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [teamId, setTeamId] = useState<string>(PERSONAL);
  const qc = useQueryClient();
  const user = useAuthStore((s) => s.user);

  // Admins can create a case for any team, not just ones they're a member of —
  // fetch the full team list for them instead of relying on `user.teams`.
  const { data: allTeams } = useQuery({
    queryKey: ["admin", "teams"],
    queryFn: adminApi.listTeams,
    enabled: !!user?.is_admin && open,
  });
  const teams = useMemo(
    () => (user?.is_admin ? (allTeams ?? []).map((t) => ({ id: t.id, name: t.name })) : manageableTeams(user)),
    [user, allTeams],
  );

  const { mutate, isPending, error } = useMutation({
    mutationFn: () =>
      casesApi.create(
        name.trim(),
        desc.trim() || undefined,
        teamId === PERSONAL ? undefined : teamId,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cases"] });
      setOpen(false);
      setName("");
      setDesc("");
      setTeamId(PERSONAL);
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="accent" size="sm">
          <Plus size={14} /> New Case
        </Button>
      </DialogTrigger>
      <DialogContent
        title="New Investigation Case"
        description="A case groups related timelines under a single investigation."
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
              Case name <span className="text-[var(--color-danger)]">*</span>
            </label>
            <Input
              placeholder="e.g. Compromised endpoint ACME-042"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              maxLength={255}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
              Description
            </label>
            <textarea
              placeholder="Short description of the investigation…"
              value={desc}
              onChange={(e) => setDesc(e.target.value)}
              maxLength={4096}
              rows={3}
              className="w-full resize-none rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] px-3 py-2 text-sm text-[var(--color-fg-primary)] placeholder:text-[var(--color-fg-muted)] focus:border-[var(--color-accent)] focus:outline-none"
            />
          </div>
          {teams.length > 0 && (
            <div>
              <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                Team (optional)
              </label>
              <Select value={teamId} onValueChange={setTeamId}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={PERSONAL}>Personal (only me)</SelectItem>
                  {teams.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {t.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                A team case is visible to every team member; a personal case is visible only to
                you and admins.
              </p>
            </div>
          )}
          {error && (
            <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={!name.trim() || isPending}
              onClick={() => mutate()}
            >
              {isPending ? "Creating…" : "Create Case"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
