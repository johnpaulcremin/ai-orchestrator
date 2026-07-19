# Routing accuracy eval

Measures how well the `auto` router classifies prompts into the **fast** vs
**smart** tier, against a labeled dataset.

- `dataset.json` — labeled prompts (`prompt`, `category`, `expected_tier`).
- `harness.py` — pure scoring logic (dataset-loading, tier extraction, accuracy
  + confusion). Injectable `decide` function, so it is unit-tested offline in
  `tests/test_evals.py` with no network.
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

Sample output (actual run of the bundled dataset, `gpt-5-nano` router):

```
Routing accuracy: 24/24 = 100.0%

Confusion (expected->predicted):
  fast->fast: 10
  smart->smart: 14
```

When the router misroutes, each miss is listed under a `Misroutes:` section with
the category, expected vs predicted tier, and the prompt.

Add your own prompts to `dataset.json` (or pass `--dataset path.json`) to track
routing quality on traffic that matters to you.
