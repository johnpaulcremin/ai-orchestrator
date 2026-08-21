import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";

function makeProps(overrides: Partial<ComponentProps<typeof Composer>> = {}) {
  return {
    attachedImages: [],
    attachedFiles: [],
    attachedAudio: [],
    removeAttachedImage: vi.fn(),
    removeAttachedFile: vi.fn(),
    removeAttachedAudio: vi.fn(),
    budgetWarning: null,
    costPreview: null,
    question: "",
    dragActive: false,
    setDragActive: vi.fn(),
    handleFilesSelected: vi.fn(async () => {}),
    fileInputRef: { current: null },
    maxAttachedImages: 4,
    maxAttachedFiles: 4,
    maxAttachedAudio: 2,
    recording: false,
    toggleRecording: vi.fn(async () => {}),
    transcribing: false,
    freeRecording: false,
    toggleFreeRecording: vi.fn(),
    researchMode: false,
    webSearchEnabled: true,
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

  it("spellchecks the question box", () => {
    render(<Composer {...makeProps({})} />);

    const textarea = screen.getByLabelText("Ask a question");
    // spellcheck is an INHERITED tri-state, so leaving it unset is not the
    // same as setting it true: an ancestor can turn it off. Asserted as the
    // literal attribute for that reason.
    expect(textarea).toHaveAttribute("spellcheck", "true");
    expect(textarea).toHaveAttribute("autocorrect", "on");
    expect(textarea).toHaveAttribute("autocapitalize", "sentences");
  });

  it("disables research mode when web search retrieval is switched off", () => {
    render(<Composer {...makeProps({ webSearchEnabled: false })} />);

    const button = screen.getByRole("button", { name: "Toggle research mode" });
    expect(button).toBeDisabled();
    // The title has to name the switch: a greyed-out button with no reason
    // is the same dead end as the silent no-op it replaces.
    expect(button).toHaveAttribute("title", expect.stringContaining("Web search retrieval"));
  });

  it("leaves research mode usable when web search retrieval is on", () => {
    render(<Composer {...makeProps({ webSearchEnabled: true })} />);

    const button = screen.getByRole("button", { name: "Toggle research mode" });
    expect(button).not.toBeDisabled();
    expect(button.getAttribute("title")).toContain("force a live web search");
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

  it("renders attached audio chips with duration and removes one on click", async () => {
    const user = userEvent.setup();
    const removeAttachedAudio = vi.fn();
    render(
      <Composer
        {...makeProps({
          attachedAudio: [
            { filename: "standup.webm", data: "data:audio/webm;base64,abc", duration_seconds: 75 },
          ],
          removeAttachedAudio,
        })}
      />,
    );

    expect(screen.getByText(/standup\.webm \(1:15\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove audio attachment standup.webm" }));
    expect(removeAttachedAudio).toHaveBeenCalledWith(0);
  });

  it("disables the attach button only once images, files, AND audio all hit their caps", () => {
    render(
      <Composer
        {...makeProps({
          attachedImages: ["data:image/png;base64,a", "data:image/png;base64,b", "data:image/png;base64,c", "data:image/png;base64,d"],
          attachedAudio: [{ filename: "a.webm", data: "data:audio/webm;base64,x" }],
          maxAttachedAudio: 2,
        })}
      />,
    );
    expect(screen.getByRole("button", { name: /Attach an image, document, or audio clip/ })).toBeEnabled();
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

  it("shows the full desktop placeholder when the viewport isn't narrow", () => {
    render(<Composer {...makeProps()} />);
    expect(screen.getByLabelText("Ask a question")).toHaveAttribute(
      "placeholder",
      "Ask inside this saved conversation... (Enter to send, Shift+Enter for a new line, Ctrl+Enter also sends)",
    );
  });

  it("shows a short placeholder below the ~850px mobile breakpoint, so it never wraps or clips", () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = (query: string) =>
      ({
        ...originalMatchMedia(query),
        matches: query === "(max-width: 850px)",
      }) as MediaQueryList;

    try {
      render(<Composer {...makeProps()} />);
      expect(screen.getByLabelText("Ask a question")).toHaveAttribute("placeholder", "Ask a question…");
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("groups attach/mic/research into .composer-tools, separate from Send", () => {
    const { container } = render(<Composer {...makeProps()} />);
    const tools = container.querySelector(".composer-tools");
    expect(tools).toContainElement(screen.getByLabelText("Attach an image, document, or audio clip"));
    expect(tools).toContainElement(screen.getByLabelText("Toggle research mode"));
    expect(tools).not.toContainElement(screen.getByRole("button", { name: /^Ask/ }));
  });

  it("reports this bar's rendered height as --composer-height, for App.css's .jump-to-bottom to stay clear of it", () => {
    const { container } = render(<Composer {...makeProps()} />);
    const bar = container.querySelector(".composer-bar") as HTMLElement;
    // jsdom performs no real layout (offsetHeight always reads 0), so this
    // only confirms the ResizeObserver wiring actually ran and set the
    // variable to this element's height -- not a meaningful pixel value.
    expect(document.documentElement.style.getPropertyValue("--composer-height")).toBe(
      `${bar.offsetHeight}px`,
    );
  });
});
