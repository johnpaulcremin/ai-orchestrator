/**
 * The browser-native writing assists every box you compose a MESSAGE in
 * should carry: spellcheck, autocorrect, and sentence capitalisation.
 *
 * Spread onto the element rather than left to the defaults, because the
 * defaults are not one thing. `spellcheck` is an inherited tri-state: unset
 * means "ask my nearest ancestor", and only if nothing up the tree has an
 * opinion does the browser fall back to its own — which differs per browser,
 * and on iOS Safari depends on a system setting. Stating it here means one
 * `spellcheck={false}` added to a wrapper later cannot silently switch it off
 * for the composer. `autoCorrect`/`autoCapitalize` are mobile-keyboard hints
 * with no desktop effect, and they genuinely default to off.
 *
 * Deliberately NOT applied to the settings/template/system-prompt textareas:
 * those hold model names, env keys and prompt fragments, where a red squiggle
 * under every identifier is noise and autocorrect actively corrupts input.
 *
 * Worth being honest about the ceiling: this is a dictionary spellchecker. It
 * flags non-words. It cannot flag a correctly-spelled wrong word — "imagine"
 * for "image", "form" for "from" — which is the class of typo that reads as a
 * capability gap when the app takes the sentence literally. The trigger-side
 * fix for that lives in app/orchestrator_tools.py's picture-noun list.
 */
export const TEXT_ENTRY_ASSISTS = {
  spellCheck: true,
  autoCorrect: "on",
  autoCapitalize: "sentences",
} as const;
