import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Library } from "./Library";

type LibraryDocument = {
  id: number;
  filename: string;
  mime_type: string;
  size_bytes: number;
  chunk_count: number;
  created_at: string;
};

function makeDocument(overrides: Partial<LibraryDocument> = {}): LibraryDocument {
  return {
    id: 1,
    filename: "notes.txt",
    mime_type: "text/plain",
    size_bytes: 1024,
    chunk_count: 2,
    created_at: "2026-07-20 10:00:00",
    ...overrides,
  };
}

let currentItems: LibraryDocument[];
let ragLibraryEnabled: boolean;
let postRequests: { body: unknown }[];
let deleteRequests: string[];
let postShouldFail: boolean;
let postStatus: number;
let deleteShouldFail: boolean;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (url.endsWith("/v1/library/documents") && method === "GET") {
        return Response.json(currentItems);
      }
      if (url.endsWith("/v1/settings") && method === "GET") {
        return Response.json({
          features: [{ key: "RAG_LIBRARY", effective_enabled: ragLibraryEnabled }],
        });
      }
      if (url.endsWith("/v1/library/documents") && method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        postRequests.push({ body });
        if (postShouldFail) {
          return Response.json({ detail: "No extractable text found." }, { status: postStatus });
        }
        return Response.json(
          {
            id: 99,
            filename: body.filename,
            mime_type: "text/plain",
            size_bytes: 42,
            chunk_count: 1,
            created_at: "2026-07-22 09:00:00",
          },
          { status: 201 },
        );
      }
      if (/\/v1\/library\/documents\/\d+$/.test(url) && method === "DELETE") {
        deleteRequests.push(url);
        if (deleteShouldFail) {
          return new Response("boom", { status: 500 });
        }
        return Response.json({ status: "deleted" });
      }
      throw new Error(`Unhandled request: ${method} ${url}`);
    }),
  );
}

const noop = () => {};
const headers = (extra: Record<string, string> = {}) => ({ ...extra });

beforeEach(() => {
  currentItems = [makeDocument()];
  ragLibraryEnabled = true;
  postRequests = [];
  deleteRequests = [];
  postShouldFail = false;
  postStatus = 422;
  deleteShouldFail = false;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Library", () => {
  it("loads and renders each uploaded document", async () => {
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText(/2 chunks/)).toBeInTheDocument();
  });

  it("shows a message when there are no documents", async () => {
    currentItems = [];
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByText(/No documents uploaded yet/i)).toBeInTheDocument();
  });

  it("shows an error message when loading fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load library/i);
  });

  it("shows a disabled-feature notice when RAG_LIBRARY is off", async () => {
    ragLibraryEnabled = false;
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    expect(
      await screen.findByText(/document library feature is currently off/i),
    ).toBeInTheDocument();
  });

  it("uploads a selected text file", async () => {
    const user = userEvent.setup();
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("notes.txt");

    const file = new File(["hello world"], "manual.txt", { type: "text/plain" });
    const input = screen.getByLabelText("Upload document");
    await user.upload(input, file);

    expect(await screen.findByText("manual.txt")).toBeInTheDocument();
    expect(postRequests).toHaveLength(1);
    expect((postRequests[0].body as { filename: string }).filename).toBe("manual.txt");
  });

  it("shows an error when uploading fails", async () => {
    postShouldFail = true;
    const user = userEvent.setup();
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("notes.txt");

    const file = new File(["  "], "empty.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("Upload document"), file);

    expect(await screen.findByRole("alert")).toHaveTextContent(/No extractable text found/i);
  });

  it("deletes a document via DELETE and drops it from the list", async () => {
    const user = userEvent.setup();
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("notes.txt");

    await user.click(screen.getByRole("button", { name: "Delete notes.txt" }));

    expect(await screen.findByText(/No documents uploaded yet/i)).toBeInTheDocument();
    expect(deleteRequests).toEqual(["/api/v1/library/documents/1"]);
  });

  it("shows an error and keeps the row when deleting fails", async () => {
    deleteShouldFail = true;
    const user = userEvent.setup();
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("notes.txt");

    await user.click(screen.getByRole("button", { name: "Delete notes.txt" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to delete document/i);
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });

  it("shows the total size of all uploaded documents", async () => {
    currentItems = [
      makeDocument({ id: 1, filename: "a.txt", size_bytes: 500 }),
      makeDocument({ id: 2, filename: "b.txt", size_bytes: 524 }),
    ];
    render(<Library apiBase="/api" getHeaders={headers} onClose={noop} />);
    await screen.findByText("a.txt");
    expect(await screen.findByText(/Total size: 1.0 KB/)).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Library apiBase="/api" getHeaders={headers} onClose={onClose} />);
    await screen.findByText("notes.txt");

    await user.click(screen.getByRole("button", { name: "Close document library" }));
    expect(onClose).toHaveBeenCalled();
  });
});
