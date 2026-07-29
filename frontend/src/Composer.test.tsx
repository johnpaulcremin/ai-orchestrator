import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";

function makeProps(overrides: Partial<ComponentProps<typeof Composer>> = {}) {
  return {
    attachedImages: [],
    attachedFiles: [],
    removeAttachedImage: vi.fn(),
    removeAttachedFile: vi.fn(),
    budgetWarning: null,
    costPreview: null,
    question: "",
    dragActive: false,
    setDragActive: vi.fn(),
    handleFilesSelected: vi.fn(async () => {}),
    fileInputRef: { current: null },
    maxAttachedImages: 4,
    maxAttachedFiles: 4,
    recording: false,
    toggleRecording: vi.fn(async () => {}),
    transcribing: false,
    freeRecording: false,
    toggleFreeRecording: vi.fn(),
    researchMode: false,
    setResearchMode: vi.fn(),
    questionInputRef: { current: null },
    setQuestion: vi.fn(),
    setCostPreview: vi.fn(),
    askQuestion: vi.fn(async () => {}),
    streaming: false,
    stopStreaming: vi.fn(),
    loading: false,
    ...overrides,
  };
}

describe("Composer", () => {
  it("renders the question textarea and calls askQuestion when Ask is clicked", async () => {
    const user = userEvent.setup();
    const askQuestion = vi.fn(async () => {});
    render(<Composer {...makeProps({ question: "Hello there", askQuestion })} />);

    expect(screen.getByLabelText("Ask a question")).toHaveValue("Hello there");
    await user.click(screen.getByRole("button", { name: "$ Ask" }));
    expect(askQuestion).toHaveBeenCalled();
  });

  it("sends the question on Enter without a shift key", async () => {
    const user = userEvent.setup();
    const askQuestion = vi.fn(async () => {});
    render(<Composer {...makeProps({ question: "Hello", askQuestion })} />);

    screen.getByLabelText("Ask a question").focus();
    await user.keyboard("{Enter}");
    expect(askQuestion).toHaveBeenCalled();
  });

  it("shows Stop instead of Ask while streaming, and calls stopStreaming", async () => {
    const user = userEvent.setup();
    const stopStreaming = vi.fn();
    render(<Composer {...makeProps({ streaming: true, stopStreaming })} />);

    expect(screen.queryByRole("button", { name: "$ Ask" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(stopStreaming).toHaveBeenCalled();
  });

  it("shows a budget warning banner when provided", () => {
    render(<Composer {...makeProps({ budgetWarning: "Only $1.00 left today." })} />);
    expect(screen.getByText(/Only \$1.00 left today\./)).toBeInTheDocument();
  });

  it("renders attached image previews and removes one on click", async () => {
    const user = userEvent.setup();
    const removeAttachedImage = vi.fn();
    render(
      <Composer
        {...makeProps({ attachedImages: ["data:image/png;base64,abc"], removeAttachedImage })}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Remove attachment 1" }));
    expect(removeAttachedImage).toHaveBeenCalledWith(0);
  });
});
