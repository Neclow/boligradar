# boligradar

Copenhagen house search tool. Queries the Boliga.dk API (aggregates Home, Nybolig, EDC, and others) and outputs a dated CSV + an interactive Plotly HTML report with map, charts, and sortable table.

## Install

Requires [pixi](https://pixi.sh). Then:

```bash
git clone git@github.com:Neclow/boligradar.git
cd boligradar
pixi install
```

## Usage

### Search with default criteria

```bash
pixi run search
```

You'll be prompted to confirm or customize the search criteria:

| Criterion | Default |
|---|---|
| Budget | 2.500.000 – 4.000.000 DKK |
| Min area | 55 m² |
| Min rooms | 2 |
| Max bike to Rådhuspladsen | 40 min |

### Dry run (check criteria without fetching)

```bash
pixi run dry-run
```

### Output

Results go to `output/`:

- `results_YYYYMMDD.csv` — semicolon-delimited, UTF-8 with BOM (opens in Excel)
- `results_YYYYMMDD.html` — self-contained interactive report

The HTML report includes:
- Map of all listings (colored by how many criteria are met)
- Price vs. area scatter plot
- Price vs. bike distance chart
- Sortable, filterable table with direct Boliga links

### Sharing

The HTML file works standalone in any browser — share it via Google Drive, Dropbox, or as a WhatsApp document attachment.

## Data sources

- Listings: [Boliga.dk](https://www.boliga.dk) API
- Park locations: [OpenStreetMap](https://www.openstreetmap.org) via Overpass API
- Bike time: estimated as straight-line distance × 1.35 / 16 km/h
