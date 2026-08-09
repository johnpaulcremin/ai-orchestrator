import { render, screen } from "@testing-library/react";
import ReactMarkdown from "react-markdown";
import { describe, expect, it } from "vitest";
import { collectGeneratedFiles, generatedFileLink } from "./generatedFileLinks";
import type { CodeResult } from "./types";

const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

function codeResults(...filenames: string[]): CodeResult[] {
  return [
    {
      code: "df.to_excel(...)",
      logs: "ok",
      images: [],
      files: filenames.map((filename) => ({
        filename,
        mime_type: XLSX_MIME,
        data: `data:${XLSX_MIME};base64,ZmFrZS0ke${filename.length}}`,
      })),
    },
  ];
}

// The markdown is rendered through the real ReactMarkdown, with the real
// component override — the failure being fixed is specifically about what
// react-markdown does to a model-written href, so stubbing it out would test
// nothing.
function renderAnswer(markdown: string, results?: CodeResult[] | null) {
  return render(
    <ReactMarkdown components={{ a: generatedFileLink(collectGeneratedFiles(results)) }}>
      {markdown}
    </ReactMarkdown>,
  );
}

describe("collectGeneratedFiles", () => {
  it("keys every generated file on the message by lowercased filename", () => {
    const files = collectGeneratedFiles(codeResults("Q3_Revenue.xlsx", "notes.csv"));
    expect([...files.keys()]).toEqual(["q3_revenue.xlsx", "notes.csv"]);
  });

  it("is empty for a message that produced nothing", () => {
    expect(collectGeneratedFiles(null).size).toBe(0);
    expect(collectGeneratedFiles([{ code: "print(1)", logs: "1" }]).size).toBe(0);
  });

  it("keeps the FIRST file when two steps emit the same name", () => {
    // Matches _ArtefactBag.produced on the backend: the earlier file is the
    // one the answer's prose was written against.
    const results = [...codeResults("report.csv"), ...codeResults("report.csv")];
    results[1].files![0].data = "data:text/csv;base64,c2Vjb25k";
    const files = collectGeneratedFiles(results);
    expect(files.get("report.csv")!.data).not.toContain("c2Vjb25k");
  });
});

describe("a file the answer names in its prose", () => {
  it("resolves a sandbox: link to the real attachment", () => {
    // The reported bug, exactly: react-markdown strips the unknown protocol
    // and leaves href="", so the download the user clicked did nothing.
    renderAnswer(
      "Download Spreadsheet: [items_14_onwards.xlsx](sandbox:/mnt/data/items_14_onwards.xlsx)",
      codeResults("items_14_onwards.xlsx"),
    );
    const link = screen.getByRole("link", { name: "items_14_onwards.xlsx" });
    expect(link).toHaveAttribute("href", expect.stringContaining("base64,"));
    expect(link).toHaveAttribute("download", "items_14_onwards.xlsx");
  });

  it("resolves a bare relative path, which would otherwise 404 inside the SPA", () => {
    renderAnswer("See [the sheet](q3_revenue.xlsx).", codeResults("q3_revenue.xlsx"));
    expect(screen.getByRole("link", { name: "the sheet" })).toHaveAttribute(
      "download",
      "q3_revenue.xlsx",
    );
  });

  it("resolves from the link's LABEL when the href names nothing", () => {
    renderAnswer(
      "[q3_revenue.xlsx](https://example.invalid/downloads/9f2c)",
      codeResults("q3_revenue.xlsx"),
    );
    expect(screen.getByRole("link", { name: "q3_revenue.xlsx" })).toHaveAttribute(
      "download",
      "q3_revenue.xlsx",
    );
  });

  it("reads a filename wrapped in inline code or bold", () => {
    renderAnswer("[`q3_revenue.xlsx`](sandbox:/mnt/data/x)", codeResults("q3_revenue.xlsx"));
    expect(screen.getByRole("link")).toHaveAttribute("download", "q3_revenue.xlsx");
  });

  it("matches case-insensitively and ignores a query string", () => {
    renderAnswer("[sheet](/mnt/data/Q3_Revenue.XLSX?v=2)", codeResults("q3_revenue.xlsx"));
    expect(screen.getByRole("link", { name: "sheet" })).toHaveAttribute(
      "download",
      "q3_revenue.xlsx",
    );
  });
});

describe("a file the answer names but does not carry", () => {
  it("renders as plain text, never as a link that goes nowhere", () => {
    // A promise of a download that cannot be kept is worse than no
    // affordance at all: the user clicks, the page reloads, nothing happens.
    renderAnswer("Download: [items_14_onwards.xlsx](sandbox:/mnt/data/items.xlsx)", null);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/items_14_onwards\.xlsx/)).toBeInTheDocument();
  });

  it("leaves a real http link alone even when it points at a file", () => {
    // A citation of somebody else's spreadsheet is a working link, and not
    // this module's business.
    renderAnswer("See [the source](https://example.com/data/figures.csv).", null);
    expect(screen.getByRole("link", { name: "the source" })).toHaveAttribute(
      "href",
      "https://example.com/data/figures.csv",
    );
  });
});

describe("ordinary links", () => {
  it("are untouched", () => {
    renderAnswer("Read [the docs](https://example.com/docs) for more.", codeResults("a.csv"));
    expect(screen.getByRole("link", { name: "the docs" })).toHaveAttribute(
      "href",
      "https://example.com/docs",
    );
  });

  it("are not mistaken for filenames when they merely contain a dot", () => {
    renderAnswer("[example.com](example.com)", null);
    expect(screen.getByRole("link", { name: "example.com" })).toBeInTheDocument();
  });
});

describe("a filename inside a longer label", () => {
  it("still resolves — the label is prose, not just a name", () => {
    // The anchored-plus-spaces regex this replaced matched the whole label,
    // looked up "download report.csv", missed, and stripped the link to
    // plain text while the file sat attached to the very same message.
    renderAnswer("[Download report.csv](sandbox:/mnt/data/report.csv)", codeResults("report.csv"));
    const link = screen.getByRole("link", { name: "Download report.csv" });
    expect(link).toHaveAttribute("download", "report.csv");
  });

  it("resolves a name followed by trailing prose", () => {
    renderAnswer("[report.csv (12 rows)](sandbox:/x)", codeResults("report.csv"));
    expect(screen.getByRole("link")).toHaveAttribute("download", "report.csv");
  });

  it("takes the file, not a directory that looks like one", () => {
    renderAnswer("[data.out/report.csv](sandbox:/x)", codeResults("report.csv"));
    expect(screen.getByRole("link")).toHaveAttribute("download", "report.csv");
  });

  it("does not leak match state between calls", () => {
    // A /g regex carries lastIndex across .test() calls; matchAll does not.
    // Two identical lookups in a row must agree.
    const files = codeResults("report.csv");
    renderAnswer("[Download report.csv](sandbox:/x)", files);
    renderAnswer("[Download report.csv](sandbox:/x)", files);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("download", "report.csv");
    expect(links[1]).toHaveAttribute("download", "report.csv");
  });

  it("still leaves a label with no filename in it alone", () => {
    renderAnswer("[the attached workbook](https://example.com/docs)", codeResults("report.csv"));
    expect(screen.getByRole("link", { name: "the attached workbook" })).toHaveAttribute(
      "href",
      "https://example.com/docs",
    );
  });
});
