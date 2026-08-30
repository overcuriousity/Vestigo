import { useState } from "react";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/Button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { downloadChartPng, downloadChartSvg, downloadCsv } from "@/components/viz/lib/export";

const SCALES = [1, 2, 3, 4] as const;

interface ExportControlsProps {
  /** Ref to the chart's `<svg>` element (from `ChartFrame`'s `svgRef`). */
  svgRef: React.RefObject<SVGSVGElement | null>;
  /** Base filename (without extension). */
  filename: string;
  /** Lines appended as a caption footer on the exported image — case,
   * timeline, field, active filters, bin params, etc. */
  captionLines?: string[];
  /** CSV text the figure already holds (the table). Non-null adds a CSV
   * format; the text is written verbatim, no SVG involved. */
  csv?: string | null;
}

type ExportFormat = "svg" | "png" | "csv";

/** Format (SVG/PNG) + resolution picker and download button, shared by the
 * per-value histogram modal and the Visualization page. */
export function ExportControls({
  svgRef,
  filename,
  captionLines = [],
  csv = null,
}: ExportControlsProps) {
  const [format, setFormat] = useState<ExportFormat>("png");
  // A CSV pick outlives the table that offered it when the analyst switches
  // figure; fall back rather than offer a download that has nothing to write.
  const effective: ExportFormat = csv == null && format === "csv" ? "png" : format;
  const [scale, setScale] = useState<(typeof SCALES)[number]>(2);
  const [busy, setBusy] = useState(false);
  // Set when the browser's canvas limits forced the PNG below the picked
  // resolution — a tall horizontal bar chart at a high `topN` reaches that at
  // any scale. Saying nothing would leave the picker reading 4× over a file
  // that is not 4×.
  const [downscaled, setDownscaled] = useState<number | null>(null);

  const handleDownload = async () => {
    if (effective === "csv") {
      if (csv != null) downloadCsv(csv, filename);
      return;
    }
    const svg = svgRef.current;
    if (!svg) return;
    setBusy(true);
    setDownscaled(null);
    try {
      if (effective === "svg") {
        downloadChartSvg(svg, filename, captionLines);
      } else {
        const used = await downloadChartPng(svg, filename, scale, captionLines);
        if (used < scale) setDownscaled(used);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      {downscaled != null && effective === "png" && (
        <span role="status" className="max-w-64 text-xs text-[var(--color-fg-muted)]">
          Exported at {downscaled < 1 ? downscaled.toFixed(2) : downscaled.toFixed(1)}× — this
          chart is too tall for a {scale}× canvas. Export as SVG for full detail.
        </span>
      )}
      <Select
        value={effective}
        onValueChange={(v) => {
          setFormat(v as ExportFormat);
          setDownscaled(null);
        }}
      >
        <SelectTrigger className="h-8 w-20 text-xs" aria-label="Export format">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="png">PNG</SelectItem>
          <SelectItem value="svg">SVG</SelectItem>
          {csv != null && <SelectItem value="csv">CSV</SelectItem>}
        </SelectContent>
      </Select>
      {effective === "png" && (
        <Select
          value={String(scale)}
          onValueChange={(v) => {
            setScale(Number(v) as (typeof SCALES)[number]);
            setDownscaled(null);
          }}
        >
          <SelectTrigger className="h-8 w-16 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCALES.map((s) => (
              <SelectItem key={s} value={String(s)}>
                {s}×
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Button
        variant="outline"
        size="sm"
        onClick={handleDownload}
        disabled={busy}
        title={
          effective === "csv"
            ? "Download table as CSV"
            : `Download chart as ${effective.toUpperCase()}`
        }
      >
        <Download size={13} />
        Export
      </Button>
    </div>
  );
}
