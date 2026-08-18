/**
 * The "Let AI write the converter" mode: hidden without the capability,
 * discloses exactly what leaves the host, hides that disclosure when a saved
 * converter is reused (nothing is sent), and starts a convert job.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UploadDialog } from "@/components/timelines/UploadDialog";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { useJobsStore } from "@/stores/jobs";

const convertMock = vi.fn();
const listMock = vi.fn();
const capsMock = vi.fn();

vi.mock("@/api/sources", () => ({
  sourcesApi: { upload: vi.fn() },
}));
vi.mock("@/api/converters", () => ({
  convertersApi: {
    convert: (...a: unknown[]) => convertMock(...a),
    listForCase: (...a: unknown[]) => listMock(...a),
  },
}));
vi.mock("@/api/agent", () => ({
  agentApi: {
    getInfo: async () => ({ model: "gpt-x", api_base_url: "https://llm.example.org/v1" }),
  },
}));
vi.mock("@/api/health", () => ({
  useCapabilities: () => capsMock(),
}));

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <UploadDialog caseId="c1" />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function openAndSwitch() {
  fireEvent.click(screen.getByRole("button", { name: /Upload Data/ }));
  fireEvent.click(screen.getByRole("button", { name: /Let AI write the converter/ }));
}

function pickFile(name = "app.log") {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File([new Uint8Array(4000)], name)] } });
}

beforeEach(() => {
  convertMock.mockReset();
  listMock.mockReset();
  capsMock.mockReset();
  useJobsStore.setState({ jobs: {} });
  listMock.mockResolvedValue({
    scripts: [
      { id: "s1", name: "syslog2vestigo", version: 2, status: "working" },
      { id: "s2", name: "broken2vestigo", version: 1, status: "failed" },
    ],
    sample_bytes: 65536,
  });
});

describe("UploadDialog — AI converter mode", () => {
  it("shows no AI mode without the capability", () => {
    capsMock.mockReturnValue({ converter_generation: false });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /Upload Data/ }));
    expect(screen.queryByText(/Let AI write the converter/)).toBeNull();
    expect(screen.getByRole("button", { name: "Upload" })).toBeTruthy();
  });

  it("discloses the sample, model and endpoint before generating", async () => {
    capsMock.mockReturnValue({ converter_generation: true });
    renderDialog();
    openAndSwitch();
    pickFile("app.log");
    const note = await screen.findByRole("note");
    await waitFor(() => expect(note.textContent).toContain("gpt-x"));
    expect(note.textContent).toContain("llm.example.org");
    expect(note.textContent).toContain("app.log");
    expect(note.textContent).toContain("64");
    expect(screen.getByRole("button", { name: "Generate & ingest" })).toBeTruthy();
  });

  it("reusing a saved converter hides the disclosure and relabels the button", async () => {
    capsMock.mockReturnValue({ converter_generation: true });
    renderDialog();
    openAndSwitch();
    const select = (await screen.findByLabelText(
      /Reuse a converter from this case/,
    )) as HTMLSelectElement;
    // Only working scripts are offered.
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      "Generate a new one",
      "syslog2vestigo v2",
    ]);
    fireEvent.change(select, { target: { value: "s1" } });
    expect(screen.getByRole("note").textContent).toContain("Nothing is sent to the model");
    expect(screen.getByRole("button", { name: "Convert & ingest" })).toBeTruthy();
  });

  it("starts the convert job with hint and reuse id and tracks it in the tray", async () => {
    capsMock.mockReturnValue({ converter_generation: true });
    convertMock.mockResolvedValue({ job_id: "job-9", converter_script_id: null });
    renderDialog();
    openAndSwitch();
    pickFile("app.log");
    fireEvent.change(await screen.findByLabelText(/Hint for the model/), {
      target: { value: "local time" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate & ingest" }));
    await waitFor(() => expect(convertMock).toHaveBeenCalledTimes(1));
    const [caseId, file, opts] = convertMock.mock.calls[0] as [string, File, Record<string, unknown>];
    expect(caseId).toBe("c1");
    expect(file.name).toBe("app.log");
    expect(opts).toEqual({ hint: "local time", converterScriptId: undefined });
    await waitFor(() => expect(useJobsStore.getState().jobs["job-9"]).toBeTruthy());
    expect(useJobsStore.getState().jobs["job-9"].label).toContain("with AI");
  });

  it("offers only saved converters when the model is unreachable (converter_reuse)", async () => {
    capsMock.mockReturnValue({ converter_generation: false, converter_reuse: true });
    convertMock.mockResolvedValue({ job_id: "job-2", converter_script_id: "s1" });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /Upload Data/ }));
    // The mode appears once the case is known to hold a working script…
    fireEvent.click(await screen.findByRole("button", { name: /Use a saved converter/ }));
    expect(screen.queryByText(/Let AI write the converter/)).toBeNull();
    // …with no "generate a new one" entry, no hint field, and a submit that
    // waits for a choice.
    const select = (await screen.findByLabelText(
      /Reuse a converter from this case/,
    )) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual([
      "Choose a saved converter…",
      "syslog2vestigo v2",
    ]);
    expect(screen.queryByLabelText(/Hint for the model/)).toBeNull();
    pickFile("app.log");
    const submit = screen.getByRole("button", { name: "Generate & ingest" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.change(select, { target: { value: "s1" } });
    fireEvent.click(screen.getByRole("button", { name: "Convert & ingest" }));
    await waitFor(() => expect(convertMock).toHaveBeenCalledTimes(1));
    const [, , opts] = convertMock.mock.calls[0] as [string, File, Record<string, unknown>];
    expect(opts).toEqual({ hint: undefined, converterScriptId: "s1" });
  });

  it("hides the converter mode when only reuse is possible and the case has no scripts", async () => {
    capsMock.mockReturnValue({ converter_generation: false, converter_reuse: true });
    listMock.mockResolvedValue({ scripts: [], sample_bytes: 65536 });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: /Upload Data/ }));
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.queryByText(/Use a saved converter/)).toBeNull();
    expect(screen.getByRole("button", { name: "Upload" })).toBeTruthy();
  });

  it("freezes the mode switch while a transfer is in flight", async () => {
    capsMock.mockReturnValue({ converter_generation: true, converter_reuse: true });
    let resolve: (v: unknown) => void = () => {};
    convertMock.mockReturnValue(new Promise((r) => (resolve = r)));
    renderDialog();
    openAndSwitch();
    pickFile("app.log");
    fireEvent.click(screen.getByRole("button", { name: "Generate & ingest" }));
    await waitFor(() => expect(convertMock).toHaveBeenCalledTimes(1));
    const other = screen.getByRole("button", { name: /Upload timeline/ }) as HTMLButtonElement;
    expect(other.disabled).toBe(true);
    // Switching is a no-op: the progress row (with its Cancel) stays mounted.
    fireEvent.click(other);
    expect(screen.getByRole("button", { name: /Cancel upload/ })).toBeTruthy();
    resolve({ job_id: "job-3", converter_script_id: null });
    await waitFor(() => expect(useJobsStore.getState().jobs["job-3"]).toBeTruthy());
  });
});
