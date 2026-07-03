import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { adminApi } from "@/api/admin";
import { ApiError } from "@/api/client";
import type { User } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Dialog, DialogClose, DialogContent, DialogTrigger } from "@/components/ui/Dialog";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { Switch } from "@/components/ui/Switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from "@/components/ui/Table";
import { useAuthStore } from "@/stores/auth";
import { fmtTimestampFull } from "@/lib/time";

export function AdminUsersPage() {
  const qc = useQueryClient();
  const me = useAuthStore((s) => s.user);
  const { data: users, isLoading } = useQuery({
    queryKey: ["admin", "users"],
    queryFn: () => adminApi.listUsers(),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["admin", "users"] });

  const toggleActive = useMutation({
    mutationFn: (u: User) => adminApi.updateUser(u.id, { is_active: !u.is_active }),
    onSuccess: invalidate,
  });

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner size={20} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-[var(--color-fg-primary)]">
          Users ({users?.length ?? 0})
        </h2>
        <CreateUserDialog />
      </div>
      <Table>
        <TableHead>
          <tr>
            <TableHeaderCell>Username</TableHeaderCell>
            <TableHeaderCell>Provider</TableHeaderCell>
            <TableHeaderCell>Admin</TableHeaderCell>
            <TableHeaderCell>Active</TableHeaderCell>
            <TableHeaderCell>Last login</TableHeaderCell>
            <TableHeaderCell />
          </tr>
        </TableHead>
        <TableBody>
          {users?.map((u) => (
            <TableRow key={u.id}>
              <TableCell>
                <div className="font-medium">{u.username}</div>
                {u.display_name && (
                  <div className="text-xs text-[var(--color-fg-muted)]">{u.display_name}</div>
                )}
                {u.must_change_password && (
                  <Badge variant="default" className="mt-1">
                    Must change password
                  </Badge>
                )}
              </TableCell>
              <TableCell className="text-xs text-[var(--color-fg-muted)]">
                {u.auth_provider}
              </TableCell>
              <TableCell>
                {u.is_admin && <ShieldCheck size={15} className="text-[var(--color-accent)]" />}
              </TableCell>
              <TableCell>
                <Switch
                  checked={u.is_active}
                  disabled={u.id === me?.id || toggleActive.isPending}
                  onCheckedChange={() => toggleActive.mutate(u)}
                />
              </TableCell>
              <TableCell className="text-xs text-[var(--color-fg-muted)]">
                {u.last_login_at ? fmtTimestampFull(u.last_login_at) : "never"}
              </TableCell>
              <TableCell>
                <div className="flex justify-end gap-1">
                  {u.auth_provider === "local" && <RotatePasswordDialog user={u} />}
                  {u.id !== me?.id && <DeleteUserDialog user={u} />}
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function CreateUserDialog() {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const qc = useQueryClient();

  const { mutate, isPending, error, reset } = useMutation({
    mutationFn: () => adminApi.createUser({ username: username.trim(), password, is_admin: isAdmin }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      setOpen(false);
      setUsername("");
      setPassword("");
      setIsAdmin(false);
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button variant="accent" size="sm">
          <Plus size={14} /> New user
        </Button>
      </DialogTrigger>
      <DialogContent title="Create user" description="Local username/password account.">
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">Username</label>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          </div>
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
              Initial password (min. 8 characters)
            </label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <label className="flex items-center gap-2 text-sm text-[var(--color-fg-secondary)]">
            <Checkbox checked={isAdmin} onCheckedChange={(c) => setIsAdmin(c === true)} />
            Grant administrator privileges
          </label>
          {error && (
            <p className="text-xs text-[var(--color-danger)]">
              {error instanceof ApiError ? error.message : "Something went wrong."}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={!username.trim() || password.length < 8 || isPending}
              onClick={() => mutate()}
            >
              {isPending ? "Creating..." : "Create"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function RotatePasswordDialog({ user }: { user: User }) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");

  const { mutate, isPending, error, isSuccess, reset } = useMutation({
    mutationFn: () => adminApi.rotatePassword(user.id, password),
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) {
          setPassword("");
          reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" title="Rotate password">
          <KeyRound size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title={`Rotate password for ${user.username}`}
        description="The user's existing sessions are revoked immediately; they must change this password on next login."
      >
        {isSuccess ? (
          <p className="text-sm text-[var(--color-fg-primary)]">Password rotated.</p>
        ) : (
          <div className="space-y-3">
            <Input
              type="password"
              placeholder="New temporary password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
            {error && (
              <p className="text-xs text-[var(--color-danger)]">
                {error instanceof ApiError ? error.message : "Something went wrong."}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <DialogClose asChild>
                <Button variant="ghost" size="sm">
                  Cancel
                </Button>
              </DialogClose>
              <Button
                variant="accent"
                size="sm"
                disabled={password.length < 8 || isPending}
                onClick={() => mutate()}
              >
                {isPending ? "Rotating..." : "Rotate"}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function DeleteUserDialog({ user }: { user: User }) {
  const [open, setOpen] = useState(false);
  const [reassignTo, setReassignTo] = useState<string | undefined>();
  const qc = useQueryClient();
  const me = useAuthStore((s) => s.user);

  const { mutate, isPending, error } = useMutation({
    mutationFn: (reassignTo?: string) => adminApi.deleteUser(user.id, reassignTo),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      setOpen(false);
    },
  });

  const needsReassignment =
    error instanceof ApiError && error.status === 409 && !reassignTo;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="text-[var(--color-fg-muted)] hover:text-[var(--color-danger)]"
          title="Delete user"
        >
          <Trash2 size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent title={`Delete ${user.username}?`}>
        <div className="space-y-3">
          {needsReassignment && me && (
            <div className="rounded border border-[var(--color-danger)]/30 bg-[var(--color-danger-dim)] p-2 text-xs text-[var(--color-danger)]">
              This user owns one or more personal cases. Reassign them to yourself (
              {me.username}) to proceed.
            </div>
          )}
          {error && (
            <p className="text-xs text-[var(--color-danger)]">
              {error instanceof ApiError ? error.message : "Something went wrong."}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            {needsReassignment ? (
              <Button
                variant="danger"
                size="sm"
                disabled={isPending}
                onClick={() => {
                  setReassignTo(me?.id);
                  mutate(me?.id);
                }}
              >
                Reassign to me &amp; delete
              </Button>
            ) : (
              <Button
                variant="danger"
                size="sm"
                disabled={isPending}
                onClick={() => mutate(reassignTo)}
              >
                {isPending ? "Deleting..." : "Delete"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
