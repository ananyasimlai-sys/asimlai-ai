# How the data is rebuilt

The site is one page plus three data files it loads:

| File | Contents |
|---|---|
| `data/core.json` | people, assets, reports, labels and build notes |
| `data/trades.json` | one entry per disclosed trade, stored as parallel columns |
| `data/prices.json` | weekly closing prices for traded tickers |

## Running a build

```
python3 -I build.py --work WORK --out OUT --prev PREVIOUS_core.json
```

It downloads the public sources into `WORK` (about 200 MB, from GitHub only; it needs `curl` and `git`), rebuilds the whole ledger and writes `OUT/data/*.json` and `OUT/build-report.json`.

- Exit code 0 and `BUILD OK`: the output passed every check and is safe to publish.
- Exit code 2 or any error: publish nothing; the reason is in `build-report.json`.

`--prev` is the `core.json` that is currently live. The build refuses to pass if any group has lost more than 3% of its trades since then, which is what a broken or truncated source looks like. Each source must also clear a minimum size.

Other options: `--offline` reuses the downloads already in `WORK`; `--today YYYY-MM-DD` builds as of another day; `--debug-rows FILE` writes every normalised row for inspection.

## Supporting files

| File | What it is |
|---|---|
| `members.json` | a snapshot of current members plus former members who appear in the data; the live roster is layered on top at build time (made by `make_members.py`) |
| `names.json` | display names, sectors and hand-made name-to-ticker aliases (made by `make_names.py` from `aliases.txt`, the S&P 500 list and the Nasdaq screener) |
| `aliases.txt` | the hand-made aliases, one per line |
