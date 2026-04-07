# CLAUDE.md

## Project

Copenhagen house search tool. Queries Boliga.dk API and outputs a CSV + interactive Plotly HTML report.

## Stack

- Python 3.14, managed with [pixi](https://pixi.sh)
- requests, pandas, plotly
- OpenStreetMap Overpass API for park proximity data

## Commands

```bash
pixi install          # set up environment
pixi run search       # interactive search (prompts for criteria)
pixi run dry-run      # preview criteria without fetching
pixi run search -- --defaults  # use default criteria, no prompt (used in CI)
```

## Output

Goes to `output/` (gitignored):
- `results_YYYYMMDD.csv` — semicolon-delimited, UTF-8 with BOM
- `results_YYYYMMDD.html` — self-contained Plotly report

## CI

GitHub Actions workflow (`.github/workflows/search.yml`) runs every 3 days and deploys the HTML to GitHub Pages.

## Key notes

- Boliga API requires a browser-like User-Agent header
- Map uses `scatter_mapbox` with `carto-positron` tiles (not `scatter_map` — MapLibre GL JS isn't bundled in Plotly's CDN)
- Bike time is estimated: haversine distance × 1.35 road factor / 16 km/h
- Park data comes from Overpass API with fallback servers; can be flaky — script handles failures gracefully
