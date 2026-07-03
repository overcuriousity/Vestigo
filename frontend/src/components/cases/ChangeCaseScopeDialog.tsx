import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { casesApi } from "@/api/cases";
import { adminApi } from "@/api/admin";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/Select";
import { Users } from "lucide-react";
import { manageableTeams } from "@/lib/caseAccess";
import { useAuthStore } from "@/stores/auth";
import type { Case } from "@/api/types";

const PERSONAL = "__personal__";

interface Props {
  case_: Case;
}

/** Lets a case's owner or team manager move it between personal and team
 * scope. Assigning to a *new* team is further gated server-side (403) to
 * managers of that target team, or admins — see `update_case_scope`. */
export function ChangeCaseScopeDialog({ case_ }: Props) {
  const [open, setOpen] = useState(false);
  const user = useAuthStore((s) => s.user);
  const qc = useQueryClient();

  const { data: allTeams } = useQuery({
    queryKey: ["admin", "teams"],
    queryFn: adminApi.listTeams,
    enabled: !!user?.is_admin && open,
  });
  const teams = useMemo(
    () => (user?.is_admin ? (allTeams ?? []).map((t) => ({ id: t.id, name: t.name })) : manageableTeams(user)),
    [user, allTeams],
  );

  const [teamId, setTeamId] = useState<string>(case_.team_id ?? PERSONAL);

  const { mutate, isPending, error } = useMutation({
    mutationFn: () => casesApi.updateScope(case_.id, teamId === PERSONAL ? undefined : teamId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: ["case", case_.id] });
      setOpen(false);
    },
  });

  const unchanged = teamId === (case_.team_id ?? PERSONAL);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setTeamId(case_.team_id ?? PERSONAL);
      }}
    >
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="opacity-0 group-hover:opacity-100 transition-base text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
          onClick={(e) => e.preventDefault()}
          title="Change case scope"
        >
          <Users size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title={`Change scope of "${case_.name}"`}
        description="Move this case between personal and a team, or to a different team."
      >
        <div className="space-y-3">
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
          <p className="text-xs text-[var(--color-fg-muted)]">
            A team case is visible to every team member; a personal case is visible only to its
            owner and admins.
          </p>
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
              disabled={unchanged || isPending}
              onClick={() => mutate()}
            >
              {isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
