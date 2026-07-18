import { describe, it, expect } from "vitest";
import { extractSseFrames } from "./sse";

describe("extractSseFrames", () => {
  it("parses a single complete frame with event and data", () => {
    const { frames, rest } = extractSseFrames('event: meta\ndata: {"a":1}\n\n');
    expect(frames).toEqual([{ event: "meta", data: '{"a":1}' }]);
    expect(rest).toBe("");
  });

  it("parses multiple frames from one buffer", () => {
    const buffer = "event: delta\ndata: hello\n\nevent: delta\ndata: world\n\n";
    const { frames, rest } = extractSseFrames(buffer);
    expect(frames).toEqual([
      { event: "delta", data: "hello" },
      { event: "delta", data: "world" },
    ]);
    expect(rest).toBe("");
  });

  it("keeps an unterminated trailing frame in rest", () => {
    const { frames, rest } = extractSseFrames("event: delta\ndata: partial");
    expect(frames).toEqual([]);
    expect(rest).toBe("event: delta\ndata: partial");
  });

  it("reassembles a frame split across two chunks", () => {
    const first = extractSseFrames("event: delta\nda");
    expect(first.frames).toEqual([]);
    const second = extractSseFrames(first.rest + 'ta: {"text":"hi"}\n\n');
    expect(second.frames).toEqual([{ event: "delta", data: '{"text":"hi"}' }]);
    expect(second.rest).toBe("");
  });

  it("handles CRLF delimiters and line breaks", () => {
    const { frames } = extractSseFrames("event: done\r\ndata: ok\r\n\r\n");
    expect(frames).toEqual([{ event: "done", data: "ok" }]);
  });

  it("strips exactly one leading space after data:", () => {
    const { frames } = extractSseFrames("data:  two-spaces\n\n");
    expect(frames[0].data).toBe(" two-spaces");
  });

  it("joins multi-line data fields with newlines", () => {
    const { frames } = extractSseFrames("event: x\ndata: line1\ndata: line2\n\n");
    expect(frames[0].data).toBe("line1\nline2");
  });

  it("defaults the event name to 'message' when none is given", () => {
    const { frames } = extractSseFrames("data: bare\n\n");
    expect(frames[0].event).toBe("message");
  });

  it("skips frames that carry no data line", () => {
    const { frames, rest } = extractSseFrames(": keep-alive comment\n\n");
    expect(frames).toEqual([]);
    expect(rest).toBe("");
  });

  it("round-trips a JSON payload that contains newlines", () => {
    // The backend json.dumps-encodes payloads, so embedded newlines are escaped
    // and cannot forge a frame boundary.
    const payload = JSON.stringify({ text: "a\nb", event: "not-a-real-event" });
    const { frames } = extractSseFrames(`event: delta\ndata: ${payload}\n\n`);
    expect(frames).toHaveLength(1);
    expect(JSON.parse(frames[0].data)).toEqual({ text: "a\nb", event: "not-a-real-event" });
  });
});
