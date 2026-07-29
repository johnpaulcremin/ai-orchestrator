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
    await user.click(screen.getByRole("button", { name: /^Ask/ }));
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

    expect(screen.queryByRole("button", { name: /^Ask/ })).not.toBeInTheDocument();
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

  it("uses the paid transcription engine by default when the mic button is clicked", async () => {
    const user = userEvent.setup();
    const toggleRecording = vi.fn(async () => {});
    const toggleFreeRecording = vi.fn();
    render(<Composer {...makeProps({ toggleRecording, toggleFreeRecording })} />);

    await user.click(screen.getByRole("button", { name: /Record a voice question/ }));
    expect(toggleRecording).toHaveBeenCalled();
    expect(toggleFreeRecording).not.toHaveBeenCalled();
  });

  it("switches the merged mic button to the free engine via the engine select", async () => {
    const user = userEvent.setup();
    const toggleRecording = vi.fn(async () => {});
    const toggleFreeRecording = vi.fn();
    render(<Composer {...makeProps({ toggleRecording, toggleFreeRecording })} />);

    await user.selectOptions(screen.getByLabelText("Voice input engine"), "free");
    await user.click(screen.getByRole("button", { name: /Record a voice question/ }));
    expect(toggleFreeRecording).toHaveBeenCalled();
    expect(toggleRecording).not.toHaveBeenCalled();
  });

  it("stops an active paid recording when the mic button is clicked again", async () => {
    const user = userEvent.setup();
    const toggleRecording = vi.fn(async () => {});
    render(<Composer {...makeProps({ recording: true, toggleRecording })} />);

    await user.click(screen.getByRole("button", { name: "Stop recording" }));
    expect(toggleRecording).toHaveBeenCalled();
  });

  it("grows the textarea height as the question gets longer, up to the 10-line cap", () => {
    const { rerender } = render(<Composer {...makeProps({ question: "short" })} />);
    const textarea = screen.getByLabelText("Ask a question") as HTMLTextAreaElement;
    // jsdom performs no real layout (scrollHeight always reads 0), so fake a
    // long pasted question's real scrollHeight directly on the node.
    Object.defineProperty(textarea, "scrollHeight", { configurable: true, value: 500 });

    rerender(<Composer {...makeProps({ question: "a very long question ".repeat(20) })} />);

    // getComputedStyle in jsdom has no real layout either, so line-height
    // falls back to this component's 24px default -- 10 lines caps growth
    // at 240px, well below the mocked 500px scrollHeight.
    expect(textarea.style.height).toBe("240px");
  });
});
