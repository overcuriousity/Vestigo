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
import { downloadChartPng, downloadChartSvg } from "@/components/viz/lib/export";

const SCALES = [1, 2, 3, 4] as const;

interface ExportControlsProps {
  /** Ref to the chart's `<svg>` element (from `ChartFrame`'s `svgRef`). */
  svgRef: React.RefObject<SVGSVGElement | null>;
  /** Base filename (without extension). */
  filename: string;
  /** Lines appended as a caption footer on the exported image — case,
   * timeline, field, active filters, bin params, etc. */
  captionLines?: string[];
}

/** Format (SVG/PNG) + resolution picker and download button, shared by the
 * per-value histogram modal and the Visualization page. */
export function ExportControls({ svgRef, filename, captionLines = [] }: ExportControlsProps) {
  const [format, setFormat] = useState<"svg" | "png">("png");
  const [scale, setScale] = useState<(typeof SCALES)[number]>(2);
  const [busy, setBusy] = useState(false);

  const handleDownload = async () => {
    const svg = svgRef.current;
    if (!svg) return;
    setBusy(true);
    try {
      if (format === "svg") {
        downloadChartSvg(svg, filename, captionLines);
      } else {
        await downloadChartPng(svg, filename, scale, captionLines);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <Select value={format} onValueChange={(v) => setFormat(v as "svg" | "png")}>
        <SelectTrigger className="h-8 w-20 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="png">PNG</SelectItem>
          <SelectItem value="svg">SVG</SelectItem>
        </SelectContent>
      </Select>
      {format === "png" && (
        <Select
          value={String(scale)}
          onValueChange={(v) => setScale(Number(v) as (typeof SCALES)[number])}
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
        title={`Download chart as ${format.toUpperCase()}`}
      >
        <Download size={13} />
        Export
      </Button>
    </div>
  );
}
