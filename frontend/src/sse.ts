export type SseFrame = {
  event: string;
  data: string;
};

/**
 * Incrementally parse Server-Sent Events out of a text buffer.
 *
 * Returns the complete frames found and the trailing `rest` that has not yet
 * been terminated by a blank line — callers keep `rest` and prepend it to the
 * next chunk. Handles both `\n\n` and `\r\n\r\n` frame delimiters and
 * multi-line `data:` fields (joined with `\n`).
 */
export function extractSseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  let rest = buffer;

  for (;;) {
    const match = /\r?\n\r?\n/.exec(rest);
    if (!match) {
      break;
    }

    const rawFrame = rest.slice(0, match.index);
    rest = rest.slice(match.index + match[0].length);

    let eventName = "message";
    const dataLines: string[] = [];

    for (const line of rawFrame.split(/\r?\n/)) {
      if (line.startsWith("event:")) {
        eventName = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        let value = line.slice("data:".length);
        if (value.startsWith(" ")) {
          value = value.slice(1);
        }
        dataLines.push(value);
      }
    }

    if (dataLines.length > 0) {
      frames.push({ event: eventName, data: dataLines.join("\n") });
    }
  }

  return { frames, rest };
}
