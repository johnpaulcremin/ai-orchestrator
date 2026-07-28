import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Templates } from "./Templates";

type Template = {
  id: number;
  name: string;
  content: string;
  created_at: string;
  updated_at: string;
};

function makeTemplate(overrides: Partial<Template> = {}): Template {
  return {
    id: 1,
    name: "Summarize",
    content: "Summarize the following:",
    created_at: "2026-07-20 10:00:00",
    updated_at: "2026-07-20 10:00:00",
    ...overrides,
  };
}

let currentItems: Template[];
let postRequests: { body: unknown }[];
let patchRequests: { url: string; body: unknown }[];
let deleteRequests: string[];
let postShouldFail: boolean;
let patchShouldFail: boolean;
let deleteShouldFail: boolean;

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      if (url.endsWith("/v1/templates") && method === "GET") {
        return Response.json(currentItems);
      }
      if (url.endsWith("/v1/templates") && method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        postRequests.push({ body });
        if (postShouldFail) {
          return new Response("boom", { status: 500 });
        }
        return Response.json(
          { id: 99, created_at: "2026-07-22 09:00:00", updated_at: "2026-07-22 09:00:00", ...body },
          { status: 201 },
        );
      }
      if (/\/v1\/templates\/\d+$/.test(url) && method === "PATCH") {
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        patchRequests.push({ url, body });
        if (patchShouldFail) {
          return new Response("boom", { status: 500 });
        }
        return Response.json({ ...makeTemplate(), ...body });
      }
      if (/\/v1\/templates\/\d+$/.test(url) && method === "DELETE") {
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
  currentItems = [makeTemplate()];
  postRequests = [];
  patchRequests = [];
  deleteRequests = [];
  postShouldFail = false;
  patchShouldFail = false;
  deleteShouldFail = false;
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Templates", () => {
  it("loads and renders each saved template", async () => {
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    expect(await screen.findByText("Summarize")).toBeInTheDocument();
    expect(screen.getByText("Summarize the following:")).toBeInTheDocument();
  });

  it("shows a message when there are no templates", async () => {
    currentItems = [];
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    expect(await screen.findByText(/No saved templates yet/i)).toBeInTheDocument();
  });

  it("shows an error message when loading fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load templates/i);
  });

  it("inserts a template's content and closes when a row is clicked", async () => {
    const onInsert = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Templates apiBase="/api" getHeaders={headers} onClose={onClose} onInsert={onInsert} />,
    );
    await user.click(await screen.findByText("Summarize"));

    expect(onInsert).toHaveBeenCalledWith("Summarize the following:");
    expect(onClose).toHaveBeenCalled();
  });

  it("creates a new template via the form", async () => {
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.type(screen.getByLabelText("New template name"), "Translate");
    await user.type(screen.getByLabelText("New template content"), "Translate to French:");
    await user.click(screen.getByRole("button", { name: "+ Save template" }));

    expect(await screen.findByText("Translate")).toBeInTheDocument();
    expect(postRequests).toEqual([
      { body: { name: "Translate", content: "Translate to French:" } },
    ]);
  });

  it("disables Save template until both fields are filled", async () => {
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    expect(screen.getByRole("button", { name: "+ Save template" })).toBeDisabled();
    await user.type(screen.getByLabelText("New template name"), "Translate");
    expect(screen.getByRole("button", { name: "+ Save template" })).toBeDisabled();
  });

  it("shows an error when creating a template fails", async () => {
    postShouldFail = true;
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.type(screen.getByLabelText("New template name"), "Translate");
    await user.type(screen.getByLabelText("New template content"), "content");
    await user.click(screen.getByRole("button", { name: "+ Save template" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to save template/i);
  });

  it("edits a template's name and content via PATCH", async () => {
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.click(screen.getByRole("button", { name: "Rename or edit Summarize" }));
    const nameInput = screen.getByLabelText("Template name");
    await user.clear(nameInput);
    await user.type(nameInput, "Summarize (short)");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Summarize (short)")).toBeInTheDocument();
    expect(patchRequests).toEqual([
      {
        url: "/api/v1/templates/1",
        body: { name: "Summarize (short)", content: "Summarize the following:" },
      },
    ]);
  });

  it("cancels an edit without saving", async () => {
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.click(screen.getByRole("button", { name: "Rename or edit Summarize" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("Summarize")).toBeInTheDocument();
    expect(patchRequests).toEqual([]);
  });

  it("deletes a template via DELETE and drops it from the list", async () => {
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.click(screen.getByRole("button", { name: "Delete Summarize" }));

    expect(await screen.findByText(/No saved templates yet/i)).toBeInTheDocument();
    expect(deleteRequests).toEqual(["/api/v1/templates/1"]);
  });

  it("shows an error and keeps the row when deleting fails", async () => {
    deleteShouldFail = true;
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.click(screen.getByRole("button", { name: "Delete Summarize" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to delete template/i);
    expect(screen.getByText("Summarize")).toBeInTheDocument();
  });

  it("does not insert when the edit/delete buttons are clicked", async () => {
    const onInsert = vi.fn();
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={noop} onInsert={onInsert} />);
    await screen.findByText("Summarize");

    await user.click(screen.getByRole("button", { name: "Rename or edit Summarize" }));
    expect(onInsert).not.toHaveBeenCalled();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Templates apiBase="/api" getHeaders={headers} onClose={onClose} onInsert={noop} />);
    await screen.findByText("Summarize");

    await user.click(screen.getByRole("button", { name: "Close templates" }));
    expect(onClose).toHaveBeenCalled();
  });
});
