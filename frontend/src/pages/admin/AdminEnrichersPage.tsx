import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UploadCloud } from "lucide-react";
import { enrichersApi, type AdminEnricherConfig } from "@/api/enrichers";
import { FileInputButton } from "@/components/ui/FileInput";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { Switch } from "@/components/ui/Switch";
import { TransferProgressRow } from "@/components/ui/TransferProgressRow";
import { useFileTransfer } from "@/hooks/useFileTransfer";
import { fmtBytes } from "@/lib/format";

function EnricherCard({ config }: { config: AdminEnricherConfig }) {
  const qc = useQueryClient();
  // Only for the progress row's denominator: the picker uploads immediately,
  // so the file itself travels through `submit` rather than through state.
  const [uploading, setUploading] = useState<File | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["admin", "enrichers", "config"] });
    qc.invalidateQueries({ queryKey: ["enrichers"] });
  };

  const autoRunMutation = useMutation({
    mutationFn: (autoRunDefault: boolean) =>
      enrichersApi.setAdminConfig(config.key, { auto_run_default: autoRunDefault }),
    onSuccess: invalidate,
  });

  // Assets are GeoLite-sized — hundreds of MB — and the install is synchronous
  // server-side, so there is no job afterwards to watch. This progress row is
  // the whole of the feedback.
  const upload = useFileTransfer<{ available: boolean; reason: string | null }, File>({
    mutationFn: (o, file) => enrichersApi.uploadAsset(config.key, file, o),
    onSuccess: () => {
      setUploading(null);
      invalidate();
    },
    onError: () => setUploading(null),
    onCancel: () => setUploading(null),
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
            pending={upload.active}
            icon={<UploadCloud size={14} className="mr-1.5" />}
            onFiles={(files) => {
              const picked = files[0];
              if (!picked) return;
              setUploading(picked);
              upload.submit(picked);
            }}
          >
            {asset.uploaded ? `Replace ${asset.name}` : `Upload ${asset.name}`}
          </FileInputButton>
          {upload.active && uploading && (
            <TransferProgressRow
              label={`Uploading ${uploading.name}`}
              state={upload.state}
              fallbackTotal={uploading.size}
              // The asset is only installed once the whole file has landed, so
              // cancelling leaves the previous one in place.
              onCancel={upload.cancel}
              cancelLabel="Cancel"
            />
          )}
          {upload.error && (
            <p className="mt-2 text-xs text-[var(--color-danger)]">{upload.error}</p>
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
