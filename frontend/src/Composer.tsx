import type { Dispatch, RefObject, SetStateAction } from "react";
import { formatCost } from "./format";
import type { FileAttachment } from "./types";

type CostPreview = {
  model: string;
  input_tokens_estimate: number;
  output_tokens_estimate: number;
  cost_usd_estimate: number | null;
} | null;

type Props = {
  attachedImages: string[];
  attachedFiles: FileAttachment[];
  removeAttachedImage: (index: number) => void;
  removeAttachedFile: (index: number) => void;
  budgetWarning: string | null;
  costPreview: CostPreview;
  question: string;
  dragActive: boolean;
  setDragActive: Dispatch<SetStateAction<boolean>>;
  handleFilesSelected: (fileList: FileList | null) => Promise<void>;
  fileInputRef: RefObject<HTMLInputElement | null>;
  maxAttachedImages: number;
  maxAttachedFiles: number;
  recording: boolean;
  toggleRecording: () => Promise<void>;
  transcribing: boolean;
  freeRecording: boolean;
  toggleFreeRecording: () => void;
  researchMode: boolean;
  setResearchMode: Dispatch<SetStateAction<boolean>>;
  questionInputRef: RefObject<HTMLTextAreaElement | null>;
  setQuestion: Dispatch<SetStateAction<string>>;
  setCostPreview: Dispatch<SetStateAction<CostPreview>>;
  askQuestion: () => Promise<void>;
  streaming: boolean;
  stopStreaming: () => void;
  loading: boolean;
};

export function Composer({
  attachedImages,
  attachedFiles,
  removeAttachedImage,
  removeAttachedFile,
  budgetWarning,
  costPreview,
  question,
  dragActive,
  setDragActive,
  handleFilesSelected,
  fileInputRef,
  maxAttachedImages,
  maxAttachedFiles,
  recording,
  toggleRecording,
  transcribing,
  freeRecording,
  toggleFreeRecording,
  researchMode,
  setResearchMode,
  questionInputRef,
  setQuestion,
  setCostPreview,
  askQuestion,
  streaming,
  stopStreaming,
  loading,
}: Props) {
  return (
    <>
      {attachedImages.length > 0 || attachedFiles.length > 0 ? (
        <div className="attached-images-preview">
          {attachedImages.map((src, index) => (
            <div className="attached-image-thumb" key={`attached-${index}`}>
              <img src={src} alt={`Attachment ${index + 1}`} />
              <button
                type="button"
                className="remove-attached-image"
                aria-label={`Remove attachment ${index + 1}`}
                onClick={() => removeAttachedImage(index)}
              >
                ×
              </button>
            </div>
          ))}
          {attachedFiles.map((file, index) => (
            <div className="attached-file-chip" key={`attached-file-${index}`}>
              <span>📄 {file.filename}</span>
              <button
                type="button"
                className="remove-attached-image"
                aria-label={`Remove attachment ${file.filename}`}
                onClick={() => removeAttachedFile(index)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {budgetWarning ? <p className="budget-warning-banner">⚠️ {budgetWarning}</p> : null}

      {costPreview && question.trim() ? (
        <p className="cost-preview" title="Worst-case estimate before sending — the actual cost may be lower.">
          ~
          {(
            costPreview.input_tokens_estimate + costPreview.output_tokens_estimate
          ).toLocaleString()}{" "}
          tokens
          {formatCost(costPreview.cost_usd_estimate)
            ? ` · up to ${formatCost(costPreview.cost_usd_estimate)}`
            : ""}{" "}
          on {costPreview.model}
        </p>
      ) : null}

      <div
        className={`composer${dragActive ? " drag-active" : ""}`}
        onDragOver={(event) => {
          // Required so the browser allows a drop here at all — without
          // this, onDrop never fires and the OS shows its "not droppable"
          // cursor instead.
          event.preventDefault();
          if (!dragActive) setDragActive(true);
        }}
        onDragLeave={(event) => {
          // Dragging over a child (the textarea, a button) also fires
          // dragleave on this element — only clear the highlight once the
          // pointer has actually left the composer, not just moved onto
          // one of its children.
          if (!event.currentTarget.contains(event.relatedTarget as Node)) {
            setDragActive(false);
          }
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragActive(false);
          void handleFilesSelected(event.dataTransfer.files);
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*,application/pdf,text/plain,.txt,.md"
          multiple
          className="visually-hidden"
          aria-label="Attach image or document"
          onChange={(event) => {
            void handleFilesSelected(event.target.files);
            event.target.value = "";
          }}
        />
        <button
          type="button"
          className="secondary-button attach-button"
          onClick={() => fileInputRef.current?.click()}
          disabled={
            attachedImages.length >= maxAttachedImages &&
            attachedFiles.length >= maxAttachedFiles
          }
          title="Attach an image or document (PDF/plain text)"
          aria-label="Attach an image or document"
        >
          📎
        </button>
        <button
          type="button"
          className={`secondary-button mic-button${recording ? " recording" : ""}`}
          onClick={() => void toggleRecording()}
          disabled={transcribing}
          title={
            recording
              ? "Stop recording"
              : "Record a voice question — AI transcription, uses paid API tokens/credits"
          }
          aria-label={recording ? "Stop recording" : "Record a voice question"}
        >
          {recording ? "⏹" : "$ 🎤"}
        </button>
        <button
          type="button"
          className={`secondary-button mic-button${freeRecording ? " recording" : ""}`}
          onClick={() => toggleFreeRecording()}
          title={
            freeRecording
              ? "Stop the free voice input"
              : "Free voice input using your browser's built-in speech recognition — on-device, lower accuracy"
          }
          aria-label={freeRecording ? "Stop the free voice input" : "Free voice input"}
        >
          {freeRecording ? "⏹" : "🗣️"}
        </button>
        <button
          type="button"
          className={`secondary-button research-button${researchMode ? " active" : ""}`}
          onClick={() => setResearchMode((current) => !current)}
          title={
            researchMode
              ? "Research mode on — this question will force a live web search"
              : "Research mode — force a live web search for this question"
          }
          aria-label="Toggle research mode"
          aria-pressed={researchMode}
        >
          🔎
        </button>
        <textarea
          ref={questionInputRef}
          value={question}
          onChange={(event) => {
            setQuestion(event.target.value);
            if (!event.target.value.trim()) setCostPreview(null);
          }}
          aria-label="Ask a question"
          placeholder="Ask inside this saved conversation... (Enter to send, Shift+Enter for a new line, Ctrl+Enter also sends)"
          onKeyDown={(event) => {
            // Ignore Enter while an IME composition is in progress, otherwise
            // confirming a CJK candidate would submit the half-typed message.
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void askQuestion();
            }
          }}
          onPaste={(event) => {
            // A copied screenshot/image has no text representation, so this
            // never interferes with a normal text paste — it only adds
            // files when the clipboard actually carries one. Goes through
            // the same handler as the 📎 picker, so it gets the identical
            // MAX_ATTACHED_IMAGES cap, preview, and skip-status messaging.
            if (event.clipboardData?.files && event.clipboardData.files.length > 0) {
              void handleFilesSelected(event.clipboardData.files);
            }
          }}
        />
        {streaming ? (
          <button className="stop-button" onClick={stopStreaming}>
            Stop
          </button>
        ) : (
          <button onClick={askQuestion} disabled={loading} title="Uses paid API tokens/credits">
            {loading ? "Working..." : "$ Ask"}
          </button>
        )}
      </div>
    </>
  );
}
