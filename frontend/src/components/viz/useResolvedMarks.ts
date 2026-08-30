/**
 * The one query that resolves a config's marks — the Visualize page and the
 * agent's card both use it, so a mark resolves identically wherever a chart
 * is drawn. Disabled when the figure does not support marks or has none.
 */
import { useQuery } from "@tanstack/react-query";
import { vizApi } from "@/api/viz";
import { busyRetry } from "@/lib/queryClient";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import type { ChartConfig } from "@/components/viz/lib/chartConfig";

export function useResolvedMarks(
  caseId: string | undefined,
  timelineId: string | undefined,
  config: ChartConfig,
) {
  const wanted = CHART_META[config.chartType].supportsMarks && config.marks.length > 0;
  const query = useQuery({
    queryKey: ["viz-marks", caseId, timelineId, config.marks],
    queryFn: () => vizApi.resolveMarks(caseId!, timelineId!, config.marks),
    enabled: wanted && !!caseId && !!timelineId,
    ...busyRetry,
  });
  // The key deliberately omits the chart type (switching time → line keeps
  // the resolution), so a disabled query still exposes the cached answer —
  // and the caption printed "mark #1 …" under a bar chart that drew nothing.
  return { ...query, data: wanted ? query.data : undefined };
}
