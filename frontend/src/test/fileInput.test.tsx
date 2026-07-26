/**
 * The file-input primitives replaced four hand-rolled `<input type="file">`
 * sites that had drifted apart. These pin the behaviors that differed.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { FileInput, FileDropZone, FileInputButton } from "@/components/ui/FileInput";

// The picker spy is on HTMLInputElement.prototype, so it must be torn down
// between tests or call counts accumulate across the file.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function fileOf(name: string, bytes = 3): File {
  return new File([new Uint8Array(bytes)], name);
}

function theInput(): HTMLInputElement {
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

describe("FileInput", () => {
  it("hands the caller an array, and clears the value so the same file can be re-picked", () => {
    // Without the reset, re-selecting the same file after a failed upload
    // fires no `change` event and the button looks dead.
    const onFiles = vi.fn();
    render(<FileInput onFiles={onFiles} />);
    const input = theInput();
    fireEvent.change(input, { target: { files: [fileOf("a.vestigo")] } });

    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(onFiles.mock.calls[0][0].map((f: File) => f.name)).toEqual(["a.vestigo"]);
    expect(input.value).toBe("");
  });

  it("keeps the native value when asked to", () => {
    const onFiles = vi.fn();
    render(<FileInput keepValue onFiles={onFiles} />);
    const input = theInput();
    const setter = vi.spyOn(input, "value", "set");
    fireEvent.change(input, { target: { files: [fileOf("a.vestigo")] } });
    expect(setter).not.toHaveBeenCalled();
  });

  it("passes through multiple files", () => {
    const onFiles = vi.fn();
    render(<FileInput multiple onFiles={onFiles} />);
    fireEvent.change(theInput(), {
      target: { files: [fileOf("a.yml"), fileOf("b.yml")] },
    });
    expect(onFiles.mock.calls[0][0]).toHaveLength(2);
  });

  it("reports an empty array when the picker was dismissed", () => {
    const onFiles = vi.fn();
    render(<FileInput onFiles={onFiles} />);
    fireEvent.change(theInput(), { target: { files: [] } });
    expect(onFiles).toHaveBeenCalledWith([]);
  });
});

describe("FileDropZone", () => {
  it("opens the picker on click and on Enter", () => {
    const click = vi.spyOn(HTMLInputElement.prototype, "click");
    render(<FileDropZone onFiles={vi.fn()} />);
    const zone = screen.getByRole("button");

    fireEvent.click(zone);
    expect(click).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(zone, { key: "Enter" });
    expect(click).toHaveBeenCalledTimes(2);
    click.mockRestore();
  });

  it("accepts a dropped file and highlights while dragging", () => {
    const onFiles = vi.fn();
    render(<FileDropZone accept=".csv,.jsonl" onFiles={onFiles} />);
    const zone = screen.getByRole("button");

    fireEvent.dragOver(zone);
    expect(zone.className).toContain("--color-accent");
    fireEvent.dragLeave(zone);

    fireEvent.drop(zone, { dataTransfer: { files: [fileOf("log.csv")] } });
    expect(onFiles.mock.calls[0][0].map((f: File) => f.name)).toEqual(["log.csv"]);
  });

  it("filters a dropped file the picker would have rejected", () => {
    // The browser enforces `accept` only in the picker, so drag-drop would
    // otherwise accept what clicking cannot.
    const onFiles = vi.fn();
    render(<FileDropZone accept=".csv" onFiles={onFiles} />);
    fireEvent.drop(screen.getByRole("button"), {
      dataTransfer: { files: [fileOf("notes.txt")] },
    });
    expect(onFiles).not.toHaveBeenCalled();
  });

  it("keeps only the first dropped file unless multiple is set", () => {
    const onFiles = vi.fn();
    render(<FileDropZone accept=".csv" onFiles={onFiles} />);
    fireEvent.drop(screen.getByRole("button"), {
      dataTransfer: { files: [fileOf("a.csv"), fileOf("b.csv")] },
    });
    expect(onFiles.mock.calls[0][0].map((f: File) => f.name)).toEqual(["a.csv"]);
  });

  it("shows the selection's name and size instead of the prompt", () => {
    render(
      <FileDropZone
        files={fileOf("big.jsonl", 2048)}
        onFiles={vi.fn()}
        prompt="Drop a file here"
      />,
    );
    expect(screen.getByText("big.jsonl")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
    expect(screen.queryByText("Drop a file here")).toBeNull();
  });

  it("does not open the picker when disabled", () => {
    const click = vi.spyOn(HTMLInputElement.prototype, "click");
    render(<FileDropZone disabled onFiles={vi.fn()} />);
    fireEvent.click(screen.getByRole("button"));
    expect(click).not.toHaveBeenCalled();
    click.mockRestore();
  });
});

describe("FileInputButton", () => {
  it("opens the picker from the button", () => {
    const click = vi.spyOn(HTMLInputElement.prototype, "click");
    render(<FileInputButton onFiles={vi.fn()}>Upload rule</FileInputButton>);
    fireEvent.click(screen.getByRole("button", { name: "Upload rule" }));
    expect(click).toHaveBeenCalledTimes(1);
    click.mockRestore();
  });

  it("swaps the label and blocks input while pending", () => {
    render(
      <FileInputButton pending onFiles={vi.fn()}>
        Upload asset
      </FileInputButton>,
    );
    expect(screen.getByRole("button", { name: "Uploading…" })).toBeDisabled();
    expect(theInput()).toBeDisabled();
  });
});
