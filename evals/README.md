# Routing accuracy eval

Measures how well the `auto` router does two things, against a labeled dataset
of 55 prompts (5 in each of the 11 task categories):

1. **Tier accuracy** — does it pick the right **fast**/**smart** tier? This is
   what matters for basic routing (cheap tasks → cheap model).
2. **Category accuracy** — does it classify the prompt into the right task
   *category* (e.g. `coding` vs `debugging`)? This matters when you set
   per-category model overrides (`MODEL_<CATEGORY>`), since a misclassification
   then sends the request to the wrong model.

- `dataset.json` — labeled prompts (`prompt`, `category`, `expected_tier`).
- `harness.py` — pure scoring logic (tier + category accuracy, per-category
  breakdown, confusion). Injectable `decide` function, so it is unit-tested
  offline in `tests/test_evals.py` with no network.
- `run.py` — CLI that runs the **real** router and prints a report.

## Run it

Makes real router calls (`OPENAI_MODEL_ROUTER`), so `OPENAI_API_KEY` must be set.

```bash
# Windows
venv/Scripts/python.exe -m evals.run

# macOS / Linux
python -m evals.run

# fail (exit 1) if accuracy drops below a threshold — useful in a nightly job
python -m evals.run --min-accuracy 0.9
```

Sample output (actual run of the bundled 55-prompt dataset, `gpt-5-nano` router):

```
Tier accuracy:     55/55 = 100.0%
Category accuracy: 49/55 = 89.1%

category             n     tier  classified
------------------ ---  -------  ----------
analysis             5    100%        80%
casual_chat          5    100%       100%
coding               5    100%       100%
creative_writing     5    100%       100%
debugging            5    100%       100%
math                 5    100%       100%
planning             5    100%       100%
quick_fact           5    100%       100%
reasoning            5    100%        40%
simple_transform     5    100%        80%
summarization        5    100%       100%

Confusion (expected->predicted tier):
  fast->fast: 20
  smart->smart: 35
```

The interesting signal is that **tier routing is perfect while category
classification is not** — e.g. `reasoning` prompts are often labeled `analysis`.
Both are smart-tier, so basic routing is unaffected, but it tells you that
splitting `MODEL_REASONING` and `MODEL_ANALYSIS` onto different models would be
unreliable. Misroutes (wrong tier) and misclassifications (wrong category, tier
possibly still right) are each listed below the table.

Add your own prompts to `dataset.json` (or pass `--dataset path.json`) to track
routing quality on traffic that matters to you.

## Semantic-cache precision eval

This routing eval predates several decision gates added later — semantic
caching, cross-conversation memory, `math_solve`, moderation. Of those,
semantic caching is the one with a genuinely new failure mode worth its own
eval: a wrong cache **match** can silently serve a confidently wrong answer
to a different question, not just cost more or answer a bit worse (the
routing eval's failure modes). Cross-conversation memory shares the same
embedding-similarity mechanism but is a much softer guarantee already (a
false positive there just adds a possibly-irrelevant snippet the model is
told to use its own judgment on, not a served answer) — not worth a
dedicated eval on the same footing. `math_solve` has no heuristic trigger to
evaluate at all (the model decides when to call it; the actual computation
is deterministic SymPy, exhaustively unit-tested in `test_math_solve.py`),
and moderation checks every question unconditionally (no gate to measure) —
its accuracy is OpenAI's own moderation model's, not this app's code.

- `semantic_cache_dataset.json` — labeled `(stored, query, should_match)`
  pairs: true paraphrases that should hit the cache, and topically-adjacent
  near-misses (same subject, different actual answer) that must not.
- `semantic_cache_harness.py` — pure scoring logic (accuracy, paraphrase hit
  rate, false-positive rate). Injectable `embed`/`cosine_similarity`, unit-
  tested offline in `tests/test_evals.py` with no network.
- `semantic_cache_run.py` — CLI that embeds every pair via the real
  embeddings API and scores them against this app's actual (or default)
  `SEMANTIC_CACHE_THRESHOLD`.

```bash
# Windows
venv/Scripts/python.exe -m evals.semantic_cache_run

# macOS / Linux
python -m evals.semantic_cache_run

# fail if ANY near-miss wrongly clears the threshold (the default), or if
# overall accuracy drops below 0.9
python -m evals.semantic_cache_run --max-false-positive-rate 0 --min-accuracy 0.9
```

The false-positive rate is the number to watch — a hit-rate miss on a true
paraphrase just costs one ordinary (uncached) model call, but a false
positive means a wrong answer for a different question. `--max-false-positive-rate`
defaults to `0`: any false positive fails the run by default, since that's
literally the risk this eval exists to catch.
