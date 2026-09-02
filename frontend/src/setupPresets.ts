// The first-run wizard's model presets, in their own module because a
// component file may export only components (react-refresh/only-export-components)
// and the test suite needs these values too.
//
// Only model names this repo already uses as defaults or examples — a preset
// must never invent an id the operator then has to debug. All three are
// OpenAI-only on purpose: the wizard asks for ONE key, so every preset has to
// work with that one key.

export type Preset = {
  id: string;
  label: string;
  blurb: string;
  values: Record<string, string>;
};

export const TIER_KEYS = [
  "OPENAI_MODEL_ROUTER",
  "OPENAI_MODEL_FAST",
  "OPENAI_MODEL_SMART",
  "OPENAI_MODEL_FALLBACK",
] as const;

export const PRESETS: Preset[] = [
  {
    id: "balanced",
    label: "Balanced (recommended)",
    blurb: "gpt-5-mini answers the everyday questions, gpt-5 takes the hard ones.",
    values: {
      OPENAI_MODEL_ROUTER: "gpt-5-nano",
      OPENAI_MODEL_FAST: "gpt-5-mini",
      OPENAI_MODEL_SMART: "gpt-5",
      OPENAI_MODEL_FALLBACK: "gpt-5-mini",
    },
  },
  {
    id: "cheapest",
    label: "Cheapest",
    blurb: "Nano and mini everywhere. Best value; the smart tier is gpt-5-mini.",
    values: {
      OPENAI_MODEL_ROUTER: "gpt-5-nano",
      OPENAI_MODEL_FAST: "gpt-5-nano",
      OPENAI_MODEL_SMART: "gpt-5-mini",
      OPENAI_MODEL_FALLBACK: "gpt-5-nano",
    },
  },
  {
    id: "quality",
    label: "Best quality",
    blurb: "gpt-5 for everything except the router. Costs the most per answer.",
    values: {
      OPENAI_MODEL_ROUTER: "gpt-5-nano",
      OPENAI_MODEL_FAST: "gpt-5",
      OPENAI_MODEL_SMART: "gpt-5",
      OPENAI_MODEL_FALLBACK: "gpt-5-mini",
    },
  },
];
