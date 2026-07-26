import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UploadCloud } from "lucide-react";
import { enrichersApi, type AdminEnricherConfig } from "@/api/enrichers";
import { FileInputButton } from "@/components/ui/FileInput";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { Switch } from "@/components/ui/Switch";

function fmtBytes(bytes: number | null): string {
  if (bytes == null) return "—";
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
}

function EnricherCard({ config }: { config: AdminEnricherConfig }) {
  const qc = useQueryClient();

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["admin", "enrichers", "config"] });
    qc.invalidateQueries({ queryKey: ["enrichers"] });
  };

  const autoRunMutation = useMutation({
    mutationFn: (autoRunDefault: boolean) =>
      enrichersApi.setAdminConfig(config.key, { auto_run_default: autoRunDefault }),
    onSuccess: invalidate,
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => enrichersApi.uploadAsset(config.key, file),
    onSuccess: invalidate,
  });

  const asset = config.asset;

  return (
    <div className="rounded-lg border border-[var(--color-border)] p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-[var(--color-fg-primary)]">
            {config.display_name}
          </p>
          <p className="text-xs text-[var(--color-fg-muted)]">{config.description}</p>
        </div>
        <Badge variant={config.available ? "accent" : "muted"}>
          {config.available ? "Available" : "Unavailable"}
        </Badge>
      </div>

      {!config.available && config.reason && (
        <p className="text-xs text-[var(--color-warning)]">{config.reason}</p>
      )}

      <div className="flex items-center justify-between gap-3 rounded border border-[var(--color-border-subtle)] px-3 py-2">
        <div>
          <p className="text-xs font-medium text-[var(--color-fg-primary)]">
            Run automatically on new ingests
          </p>
          <p className="text-xs text-[var(--color-fg-muted)]">
            Instance-wide default. Applies to every timeline without its own enricher
            configuration; per-timeline settings override this.
          </p>
        </div>
        <Switch
          checked={config.auto_run_default}
          disabled={autoRunMutation.isPending}
          onCheckedChange={(v) => autoRunMutation.mutate(v)}
        />
      </div>

      {asset && (
        <div className="space-y-2">
          <div className="text-xs text-[var(--color-fg-muted)]">
            <span className="font-medium text-[var(--color-fg-primary)]">{asset.name}</span>
            {" — "}
            {asset.uploaded ? `uploaded (${fmtBytes(asset.size_bytes)})` : "not uploaded"}
          </div>
          <p className="text-xs text-[var(--color-fg-muted)]">{asset.description}</p>
          <FileInputButton
            variant="ghost"
            accept={asset.accepted_extensions.join(",")}
            pending={uploadMutation.isPending}
            icon={<UploadCloud size={14} className="mr-1.5" />}
            onFiles={(files) => {
              if (files[0]) uploadMutation.mutate(files[0]);
            }}
          >
            {asset.uploaded ? `Replace ${asset.name}` : `Upload ${asset.name}`}
          </FileInputButton>
          {uploadMutation.isError && (
            <p className="mt-2 text-xs text-[var(--color-danger)]">
              {(uploadMutation.error as Error).message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function AdminEnrichersPage() {
  const { data: configs, isLoading } = useQuery({
    queryKey: ["admin", "enrichers", "config"],
    queryFn: () => enrichersApi.adminConfigs(),
  });

  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold text-[var(--color-fg-primary)]">Enrichers</h2>
      {isLoading && <Spinner size={16} />}
      {configs?.map((config) => <EnricherCard key={config.key} config={config} />)}
      {configs && configs.length === 0 && (
        <p className="text-xs text-[var(--color-fg-muted)]">No enrichers registered.</p>
      )}
    </div>
  );
}
