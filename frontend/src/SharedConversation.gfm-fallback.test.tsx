import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { SharedConversation as SharedConversationType } from "./SharedConversation";

// Same regression as MessageList.gfm-fallback.test.tsx: remark-gfm crashes
// rendering on Safari < 16.4 via a hardcoded lookbehind regex. The public
// share view is exactly where an anonymous recipient (possibly on an old
// device, no login to retry) would hit this with no recovery path.
async function freshSharedConversation(
  supportsRegexLookbehind: boolean,
): Promise<typeof SharedConversationType> {
  vi.resetModules();
  vi.doMock("./markdownSupport", () => ({ supportsRegexLookbehind }));
  const mod = await import("./SharedConversation");
  return mod.SharedConversation;
}

function setPath(path: string) {
  window.history.pushState({}, "", path);
}

function stubFetch(handler: (url: string) => Response | Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      return handler(url);
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.doUnmock("./markdownSupport");
  window.history.pushState({}, "", "/");
});

describe("SharedConversation GFM fallback (remark-gfm lookbehind crash)", () => {
  it("renders without throwing and shows plain text when lookbehind is unsupported", async () => {
    setPath("/shared/old-safari-token");
    stubFetch(() =>
      Response.json({
        title: "Old phone test",
        created_at: "2026-07-18 10:00:00",
        messages: [
          {
            role: "assistant",
            content: "~~struck~~ **bold**",
            created_at: "2026-07-18 10:01:00",
          },
        ],
      }),
    );
    const SharedConversation = await freshSharedConversation(false);

    render(<SharedConversation />);

    expect(await screen.findByRole("heading", { name: "Old phone test" })).toBeInTheDocument();
    // Plain CommonMark still renders (bold is CommonMark core, not GFM) --
    // the page isn't blank.
    expect(screen.getByText("bold")).toBeInTheDocument();
    expect(document.querySelector("del")).toBeNull();
  });
});
