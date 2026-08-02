import { useEffect, useState, type Dispatch, type RefObject, type SetStateAction } from "react";
import { ArrowUp, Globe, Mic, Paperclip, Square, X } from "lucide-react";
import { formatCost } from "./format";
import { Button } from "./Button";
import type { AudioAttachment, FileAttachment } from "./types";

function formatDuration(seconds?: number | null): string | null {
  if (seconds == null || !Number.isFinite(seconds)) {
    return null;
  }
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

type CostPreview = {
  model: string;
  input_tokens_estimate: number;
  output_tokens_estimate: number;
  cost_usd_estimate: number | null;
} | null;

type MicEngine = "paid" | "free";

type Props = {
  attachedImages: string[];
  attachedFiles: FileAttachment[];
  attachedAudio: AudioAttachment[];
  removeAttachedImage: (index: number) => void;
  removeAttachedFile: (index: number) => void;
  removeAttachedAudio: (index: number) => void;
  budgetWarning: string | null;
  costPreview: CostPreview;
  question: string;
  dragActive: boolean;
  setDragActive: Dispatch<SetStateAction<boolean>>;
  handleFilesSelected: (fileList: FileList | null) => Promise<void>;
  fileInputRef: RefObject<HTMLInputElement | null>;
  maxAttachedImages: number;
  maxAttachedFiles: number;
  maxAttachedAudio: number;
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

// The auto-grow ceiling: past 10 lines the textarea stops growing and
// scrolls internally instead, so a long pasted question can't push the
// composer (and the Ask button) off the bottom of the screen.
const MAX_TEXTAREA_LINES = 10;

export function Composer({
  attachedImages,
  attachedFiles,
  attachedAudio,
  removeAttachedImage,
  removeAttachedFile,
  removeAttachedAudio,
  budgetWarning,
  costPreview,
  question,
  dragActive,
  setDragActive,
  handleFilesSelected,
  fileInputRef,
  maxAttachedImages,
  maxAttachedFiles,
  maxAttachedAudio,
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
  // Which engine the merged mic button uses -- a pure UI preference, not
  // lifted to App.tsx since nothing outside this component needs it.
  const [micEngine, setMicEngine] = useState<MicEngine>("paid");
  const micActive = recording || freeRecording;

  // Grows the textarea from one line up to MAX_TEXTAREA_LINES as the
  // question gets longer, then hands off to its own internal scroll --
  // recalculated on every keystroke since scrollHeight only reflects the
  // current content after a reset to "auto" clears the previous fixed height.
  useEffect(() => {
    const el = questionInputRef.current;
    if (!el) return;
    if (!question) {
      // scrollHeight on an EMPTY textarea reflects the placeholder's
      // wrapped line count in a narrow composer (this one is long), not any
      // real content -- falls back to the CSS single-line default instead
      // of trusting it, or the composer would render tall with nothing typed.
      el.style.height = "";
      return;
    }
    const lineHeight = parseFloat(getComputedStyle(el).lineHeight) || 24;
    const maxHeight = lineHeight * MAX_TEXTAREA_LINES;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [question, questionInputRef]);

  function toggleMic() {
    if (recording || freeRecording) {
      if (recording) void toggleRecording();
      if (freeRecording) toggleFreeRecording();
      return;
    }
    if (micEngine === "paid") {
      void toggleRecording();
    } else {
      toggleFreeRecording();
    }
  }

  return (
    <>
      {attachedImages.length > 0 || attachedFiles.length > 0 || attachedAudio.length > 0 ? (
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
                <X size={14} />
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
                <X size={14} />
              </button>
            </div>
          ))}
          {attachedAudio.map((clip, index) => (
            <div className="attached-file-chip attached-audio-chip" key={`attached-audio-${index}`}>
              <span>
                🎙️ {clip.filename}
                {formatDuration(clip.duration_seconds) ? ` (${formatDuration(clip.duration_seconds)})` : ""}
              </span>
              <button
                type="button"
                className="remove-attached-image"
                aria-label={`Remove audio attachment ${clip.filename}`}
                onClick={() => removeAttachedAudio(index)}
              >
                <X size={14} />
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
          accept="image/*,application/pdf,text/plain,.txt,.md,audio/webm,audio/wav,audio/mp3,audio/mpeg,audio/mp4,audio/m4a,audio/ogg,.m4a"
          multiple
          className="visually-hidden"
          aria-label="Attach image, document, or audio"
          onChange={(event) => {
            void handleFilesSelected(event.target.files);
            event.target.value = "";
          }}
        />

        <textarea
          ref={questionInputRef}
          value={question}
          rows={1}
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
            // the same handler as the attach picker, so it gets the identical
            // MAX_ATTACHED_IMAGES cap, preview, and skip-status messaging.
            if (event.clipboardData?.files && event.clipboardData.files.length > 0) {
              void handleFilesSelected(event.clipboardData.files);
            }
          }}
        />

        <div className="composer-actions">
          <Button
            type="button"
            iconOnly
            size="sm"
            variant="ghost"
            className="attach-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={
              attachedImages.length >= maxAttachedImages &&
              attachedFiles.length >= maxAttachedFiles &&
              attachedAudio.length >= maxAttachedAudio
            }
            aria-label="Attach an image, document, or audio clip"
            title="Attach an image, document (PDF/plain text), or audio clip (meeting recording, voice memo)"
            icon={<Paperclip size={16} />}
          />

          <div className="mic-control">
            <Button
              type="button"
              iconOnly
              size="sm"
              variant="ghost"
              className={`mic-button${micActive ? " recording" : ""}`}
              onClick={toggleMic}
              disabled={transcribing}
              aria-label={
                micActive ? "Stop recording" : `Record a voice question (${micEngine === "paid" ? "AI transcription" : "free, on-device"})`
              }
              title={
                micActive
                  ? "Stop recording"
                  : micEngine === "paid"
                    ? "Record a voice question — AI transcription, uses paid API tokens/credits"
                    : "Record a voice question — your browser's built-in speech recognition, on-device, lower accuracy"
              }
              icon={micActive ? <Square size={16} /> : <Mic size={16} />}
            />
            <select
              className="mic-engine-select"
              aria-label="Voice input engine"
              value={micEngine}
              disabled={micActive}
              onChange={(event) => setMicEngine(event.target.value as MicEngine)}
              title="Choose the voice-input engine"
            >
              <option value="paid">$ AI</option>
              <option value="free">Free</option>
            </select>
          </div>

          <Button
            type="button"
            iconOnly
            size="sm"
            variant={researchMode ? "secondary" : "ghost"}
            className={`research-button${researchMode ? " active" : ""}`}
            onClick={() => setResearchMode((current) => !current)}
            aria-label="Toggle research mode"
            aria-pressed={researchMode}
            title={
              researchMode
                ? "Research mode on — this question will force a live web search"
                : "Research mode — force a live web search for this question"
            }
            icon={<Globe size={16} />}
          />

          {streaming ? (
            <Button type="button" variant="danger" size="sm" className="stop-button" onClick={stopStreaming}>
              <Square size={14} /> Stop
            </Button>
          ) : (
            <Button
              type="button"
              variant="primary"
              size="sm"
              iconOnly
              onClick={askQuestion}
              disabled={loading}
              aria-label={loading ? "Working" : "Ask — uses paid API tokens/credits"}
              title="Uses paid API tokens/credits"
              icon={<ArrowUp size={16} />}
            />
          )}
        </div>
      </div>
    </>
  );
}
