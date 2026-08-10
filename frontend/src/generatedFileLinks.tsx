import { isValidElement, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { defaultUrlTransform } from "react-markdown";
import type { CodeFile, CodeResult } from "./types";

// A file an answer names in its own PROSE, resolved against the files that
// answer actually carries.
//
// The bug this exists for, observed live: a workflow answered "📊 Download
// Spreadsheet: items_14_onwards.xlsx" with the filename as a markdown link,
// and the link went nowhere. There is no URL a model could write that would
// have worked — a generated file reaches the browser as a data: URI inside
// `code_results[].files`, never as a path — so every such link is dead by
// construction:
//
//   [report.xlsx](sandbox:/mnt/data/report.xlsx)  react-markdown strips the
//                                                 unknown protocol, leaving
//                                                 href="" — a click reloads
//                                                 the page
//   [report.xlsx](report.xlsx)                    a relative path into the
//                                                 SPA — a 404, or the app's
//                                                 own index.html
//
// app/workflow.py's synthesis prompt now tells the model not to write one at
// all, which is the real fix. This is the guard behind it, because a prompt
// is not a guarantee: the link either resolves to the real attachment or
// stops pretending to be a link.

// react-markdown's default urlTransform drops any protocol outside its safe
// list, and `sandbox:` is not on it — so `[Download the CSV](sandbox:/mnt/
// data/report.csv)` reaches the `a` renderer below with href="" and the
// filename already gone. Every case the renderer resolved until now happened
// to carry the name in the LABEL as well ("[report.csv](sandbox:/...)"), which
// is why this went unnoticed: with a purely descriptive label there is nothing
// left to match on, and the link renders dead.
//
// Observed live: "[Download the Excel workbook](sandbox:/mnt/data/
// app_cons_improvements_workbook.xlsx)" — a click reloaded the page.
//
// Preserving the URL is what lets the renderer read the name. It never becomes
// a real href: `sandbox:` is absent from USABLE_HREF_RE, so the renderer either
// swaps in the attachment's data: URI or strips the link to plain text. Only
// this one inert scheme is preserved — everything else, `javascript:` above
// all, still goes through the default transform.
const SANDBOX_URL_RE = /^\s*sandbox:/i;

export function preserveSandboxUrls(url: string): string {
  return SANDBOX_URL_RE.test(url) ? url : defaultUrlTransform(url);
}

// A filename-shaped token, SEARCHED for rather than whole-matched — and both
// halves of that matter.
//
// The extension allowlist keeps ordinary prose ("see example.com") from being
// mistaken for a filename. Searching keeps a filename findable inside a longer
// label: a model writes "[Download report.csv](sandbox:/...)" about as often
// as it writes the bare name, and the anchored pattern this replaced ALSO
// allowed spaces — so it matched the whole label, produced the key
// "download report.csv", found nothing under it, and (the href being empty
// once react-markdown strips `sandbox:`) fell through to the
// strip-to-plain-text branch. The file was attached and the reader got no
// link to it.
//
// No spaces in the stem, deliberately, and app/workflow.py's _FILENAME_RE
// gives the same reason: a greedier pattern turns "the file tier costs.csv"
// into one 15-character "filename" that matches nothing. A generated file with
// a space in its name is rarer than a label with a word in front of it, and
// only the second was being got wrong.
const GENERATED_FILE_RE =
  /[\w][\w\-.()]*\.(csv|xlsx|xls|json|txt|md|tsv|png|jpe?g|svg|pdf|docx)\b/gi;

// Hrefs that go somewhere on their own. Anything else a model writes for a
// file it produced — a bare "report.xlsx", "/mnt/data/report.xlsx", or the
// empty string react-markdown leaves after stripping an unknown protocol —
// leads nowhere, and is only ever rendered as a link here when it resolves
// to a real attachment.
const USABLE_HREF_RE = /^(https?:|mailto:|tel:|data:|#)/i;

/** Every generated file on one message, keyed by lowercased filename.
 *
 * First writer wins on a duplicate name, matching `_ArtefactBag.produced` on
 * the backend: when two workflow steps emit the same filename, the earlier
 * one is the file the answer's prose was written against. */
export function collectGeneratedFiles(
  results?: CodeResult[] | null,
): Map<string, CodeFile> {
  const files = new Map<string, CodeFile>();
  for (const result of results ?? []) {
    for (const file of result.files ?? []) {
      const name = file.filename?.trim().toLowerCase();
      if (name && !files.has(name)) {
        files.set(name, file);
      }
    }
  }
  return files;
}

/** The text a link renders as, flattened out of its React children — the
 * link's label is often the filename, and it is routinely wrapped in `code`
 * or `strong` rather than being a bare string. */
function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(nodeText).join("");
  }
  if (isValidElement(node)) {
    return nodeText((node.props as { children?: ReactNode }).children);
  }
  return "";
}

/** The filename `value` refers to (lowercased), or null if it names no file.
 *
 * Directory prefixes and any query/fragment are dropped first, because a
 * model writes "sandbox:/mnt/data/report.xlsx" and "report.xlsx" for the
 * same file and only the basename is ever the artefact's real name. */
function fileNameIn(value: string): string | null {
  const path = value.trim().split(/[?#]/)[0] ?? "";
  const raw = path.split(/[/\\]/).pop() ?? "";
  let base = raw;
  try {
    base = decodeURIComponent(raw);
  } catch {
    // A stray % is not an escape and not a filename either — fall back to
    // the undecoded text rather than dropping the candidate entirely.
  }
  // Last match wins: a path written into the LABEL rather than the href
  // ("data/out/report.csv") should resolve to the file, not to a directory
  // that happens to look like one. `matchAll` avoids the lastIndex state a
  // /g regex carries between .test() calls.
  const matches = [...base.matchAll(GENERATED_FILE_RE)];
  return matches.length ? matches[matches.length - 1][0].toLowerCase() : null;
}

/** ReactMarkdown's `a` renderer, bound to one message's generated files.
 *
 * Three outcomes, in order:
 *  - the link names a file this message carries -> a real download of that
 *    file, whatever the model wrote as the href;
 *  - it names a file that is NOT here, and its href leads nowhere -> the
 *    label as plain text, so a promise of a download never renders as one;
 *  - anything else (an ordinary web link, a citation) -> untouched. */
export function generatedFileLink(files: Map<string, CodeFile>) {
  return function GeneratedFileLink({
    href,
    children,
    ...rest
  }: ComponentPropsWithoutRef<"a">) {
    // Both the href and the label are consulted: a model writes the name in
    // one or the other about equally often ("[report.xlsx](sandbox:/...)"
    // vs "[the spreadsheet](report.xlsx)").
    const candidates = [fileNameIn(href ?? ""), fileNameIn(nodeText(children))];
    const named = candidates.find((name) => name !== null) ?? null;
    const file = candidates
      .map((name) => (name === null ? undefined : files.get(name)))
      .find((match) => match !== undefined);

    if (file) {
      return (
        <a
          {...rest}
          className="generated-file-link"
          href={file.data}
          download={file.filename}
        >
          {children}
        </a>
      );
    }
    if (named !== null && !USABLE_HREF_RE.test((href ?? "").trim())) {
      return <>{children}</>;
    }
    return (
      <a {...rest} href={href}>
        {children}
      </a>
    );
  };
}
