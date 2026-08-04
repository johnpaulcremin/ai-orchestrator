// remark-gfm's autolink-literal extension (mdast-util-gfm-autolink-literal)
// hardcodes a regex lookbehind assertion `(?<=...)` with no option to
// disable just that sub-feature -- Safari didn't support lookbehind until
// 16.4 (2023), so on any older Safari (including iOS 15/16.0-16.3) this
// throws "Invalid regular expression: invalid group specifier name" the
// moment ANY message is rendered, taking the whole app down to a blank
// screen (React has already unmounted by the time an ErrorBoundary catches
// it further up the tree).
//
// Feature-detected ONCE at module load via a safely-caught runtime
// RegExp(string) construction -- never a lookbehind LITERAL in this file
// itself, which would be a syntax error at parse time on the very engines
// being detected, crashing before the try/catch could run.
//
// remarkPlugins={gfmPluginsIfSupported} degrades gracefully: GFM extras
// (tables, strikethrough, autolinks, task lists, footnotes) are simply
// absent on an unsupported browser -- plain CommonMark still renders --
// rather than the whole message list crashing.
function detectLookbehindSupport(): boolean {
  try {
    // Constructed from a string deliberately, not a literal -- see module
    // docstring above. Actually EXERCISED (.test(), whose boolean result is
    // returned) rather than merely constructed: a minifier that sees
    // `new RegExp(...)` as an unused expression statement can (and, in
    // testing, DOES -- esbuild's production minify pass) eliminate it as
    // dead code on the assumption that a RegExp constructor call has no
    // side effects worth keeping, silently defeating the whole detection
    // (the try block collapses to a bare `return true`). Depending on the
    // actual match result, not just constructor success, forces the call to
    // survive minification.
    return new RegExp("(?<=x)y").test("xy");
  } catch {
    return false;
  }
}

export const supportsRegexLookbehind = detectLookbehindSupport();
