import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { sanitizeFilename, triggerDownload } from "@/lib/download";

describe("sanitizeFilename", () => {
  it("replaces path separators from unix file-path field values", () => {
    expect(sanitizeFilename("attr:file_path_/var/log/audit/audit.log_histogram")).toBe(
      "attr_file_path_var_log_audit_audit.log_histogram",
    );
  });

  it("replaces windows drive/backslash characters", () => {
    expect(sanitizeFilename('C:\\Windows\\System32\\cmd.exe')).toBe(
      "C_Windows_System32_cmd.exe",
    );
  });

  it("replaces the remaining windows-illegal characters", () => {
    expect(sanitizeFilename('a*b?c"d<e>f|g')).toBe("a_b_c_d_e_f_g");
  });

  it("collapses runs and trims leading/trailing separators", () => {
    expect(sanitizeFilename("//weird//name//")).toBe("weird_name");
  });

  it("keeps ordinary names and extensions untouched", () => {
    expect(sanitizeFilename("status_code_bar.svg")).toBe("status_code_bar.svg");
  });

  it("falls back when nothing survives", () => {
    expect(sanitizeFilename("///")).toBe("download");
  });
});

describe("triggerDownload", () => {
  let originalCreateObjectURL: typeof URL.createObjectURL;
  let originalRevokeObjectURL: typeof URL.revokeObjectURL;
  let clickedDownload: string | null;

  beforeEach(() => {
    clickedDownload = null;
    originalCreateObjectURL = URL.createObjectURL;
    originalRevokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:mock-url") as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedDownload = this.download;
    });
  });

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    vi.restoreAllMocks();
  });

  it("sanitizes the filename it puts on the anchor", () => {
    triggerDownload(new Blob(["x"]), "attr:path_/etc/passwd_histogram.svg");
    expect(clickedDownload).toBe("attr_path_etc_passwd_histogram.svg");
  });
});
