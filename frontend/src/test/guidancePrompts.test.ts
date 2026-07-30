import { describe, expect, it } from "vitest";
import { guidance } from "@/lib/guidance";

describe("converter LLM prompts track the data contract", () => {
  it("Parquet prompt documents the 1.3.0 forensic footer keys", () => {
    const p = guidance.converters.llmPromptParquet;
    expect(p).toContain("vestigo.format_version");
    expect(p).toContain("vestigo.converter_name");
    expect(p).toContain("vestigo.converter_version");
    expect(p).toContain("vestigo.original_files");
    expect(p).toContain("vestigo.converted_at");
    expect(p).toContain("vestigo.row_counts");
    expect(p).toContain("vestigo.timezone_assumption");
    expect(p).toContain("vestigo.parse_decisions");
    // original_files entries carry path + mtime since converter 1.3.0.
    expect(p).toContain('"path"');
    expect(p).toContain('"mtime"');
  });

  it("Parquet prompt routes timezone assumptions into the footer, not a comment", () => {
    const p = guidance.converters.llmPromptParquet;
    expect(p).not.toContain("document any input-timezone assumption at the top of the script");
  });

  it("CSV prompt documents pipe-separated tags", () => {
    expect(guidance.converters.llmPromptCsv).toContain("pipe-separated");
  });
});
