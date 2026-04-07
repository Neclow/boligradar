#!/usr/bin/env python3
"""
Danish house search — queries Boliga.dk API (aggregates Home, Nybolig, EDC, etc.)
and produces a dated CSV + an interactive Plotly HTML report.

Run with defaults:   pixi run search
Dry-run (no fetch):  pixi run search -- --dry-run
"""

import argparse
import csv
import math
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests

# ── Defaults ─────────────────────────────────────────────────────────────────

# Rådhuspladsen, Copenhagen
TARGET_LAT = 55.6761
TARGET_LON = 12.5683

# Municipalities within ~40 min bike of Rådhuspladsen
MUNICIPALITIES = [
    101,  # København
    147,  # Frederiksberg
    157,  # Gentofte
    159,  # Gladsaxe
    167,  # Hvidovre
    173,  # Lyngby-Taarbæk
    175,  # Rødovre
    185,  # Tårnby
    153,  # Brøndby
    161,  # Glostrup
    163,  # Herlev
]

# Property types: Villa, Rækkehus, Ejerlejlighed, Andelsbolig, Villalejlighed
PROPERTY_TYPES = "1,2,3,6,8"

DEFAULT_CRITERIA = {
    "budget_min": 2_500_000,
    "budget_max": 4_000_000,
    "min_sqm": 55,
    "min_bedrooms": 2,
    "max_bike_min": 40,
}

# Average Copenhagen cycling speed (km/h) — conservative city estimate
AVG_BIKE_SPEED_KMH = 16
# Road detour factor over straight-line distance
ROAD_FACTOR = 1.35

PAGE_SIZE = 200
OUTPUT_DIR = Path(__file__).parent / "output"

BOLIGA_API = "https://api.boliga.dk/api/v2/search/results"
OVERPASS_APIS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

PROPERTY_TYPE_NAMES = {
    1: "Villa",
    2: "Rækkehus",
    3: "Ejerlejlighed",
    4: "Fritidshus",
    5: "Landejendom",
    6: "Andelsbolig",
    7: "Grund",
    8: "Villalejlighed",
}


# ── CLI & interactive input ──────────────────────────────────────────────────

def prompt_criteria() -> dict:
    """Ask the user whether to use defaults or enter custom criteria."""
    print("\n📋 Søgekriterier")
    print("─" * 40)
    print(f"  Budget min:       {DEFAULT_CRITERIA['budget_min']:>12,} DKK".replace(",", "."))
    print(f"  Budget max:       {DEFAULT_CRITERIA['budget_max']:>12,} DKK".replace(",", "."))
    print(f"  Min areal:        {DEFAULT_CRITERIA['min_sqm']:>12} m²")
    print(f"  Min værelser:     {DEFAULT_CRITERIA['min_bedrooms']:>12}")
    print(f"  Max cykel (min):  {DEFAULT_CRITERIA['max_bike_min']:>12}")
    print()

    answer = input("Kør med standardkriterier? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes", "ja"):
        return dict(DEFAULT_CRITERIA)

    criteria = {}
    criteria["budget_min"] = _ask_int("  Budget min (DKK)", DEFAULT_CRITERIA["budget_min"])
    criteria["budget_max"] = _ask_int("  Budget max (DKK)", DEFAULT_CRITERIA["budget_max"])
    criteria["min_sqm"] = _ask_int("  Min areal (m²)", DEFAULT_CRITERIA["min_sqm"])
    criteria["min_bedrooms"] = _ask_int("  Min værelser", DEFAULT_CRITERIA["min_bedrooms"])
    criteria["max_bike_min"] = _ask_int("  Max cykel til Rådhuspladsen (min)", DEFAULT_CRITERIA["max_bike_min"])
    return criteria


def _ask_int(label: str, default: int) -> int:
    """Prompt for an integer, showing the default."""
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"    ⚠ Ugyldigt tal, bruger standard: {default}")
        return default


def print_criteria(criteria: dict) -> None:
    """Pretty-print the active criteria."""
    print("\n✅ Aktive kriterier:")
    print("─" * 40)
    print(f"  Budget:          {criteria['budget_min']:>10,} – {criteria['budget_max']:,} DKK".replace(",", "."))
    print(f"  Min areal:       {criteria['min_sqm']:>10} m²")
    print(f"  Min værelser:    {criteria['min_bedrooms']:>10}")
    print(f"  Max cykelafstand:{criteria['max_bike_min']:>10} min")
    print("─" * 40)


# ── Helpers ──────────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def estimate_bike_minutes(lat: float, lon: float) -> float:
    """Estimate cycling time to Rådhuspladsen in minutes."""
    straight = haversine_km(lat, lon, TARGET_LAT, TARGET_LON)
    road_km = straight * ROAD_FACTOR
    return (road_km / AVG_BIKE_SPEED_KMH) * 60


def fetch_parks_in_area() -> list[tuple[float, float]]:
    """Fetch park/green-space centroids in Greater Copenhagen from OSM."""
    query = """
    [out:json][timeout:60];
    (
      way["leisure"="park"](55.55,12.25,55.80,12.70);
      relation["leisure"="park"](55.55,12.25,55.80,12.70);
      way["landuse"="recreation_ground"](55.55,12.25,55.80,12.70);
      way["leisure"="garden"](55.55,12.25,55.80,12.70);
    );
    out center;
    """
    for api_url in OVERPASS_APIS:
        try:
            print(f"  Trying {api_url} …")
            r = requests.post(api_url, data={"data": query}, timeout=90)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            parks = []
            for e in elements:
                if "center" in e:
                    parks.append((e["center"]["lat"], e["center"]["lon"]))
                elif "lat" in e and "lon" in e:
                    parks.append((e["lat"], e["lon"]))
            print(f"  Fetched {len(parks)} parks/green spaces from OpenStreetMap")
            return parks
        except Exception as exc:
            print(f"  ⚠ {api_url} failed: {exc}")
    print("  ⚠ All Overpass servers failed — park distances will be empty")
    return []


def nearest_park_km(lat: float, lon: float, parks: list[tuple[float, float]]) -> float | None:
    if not parks:
        return None
    return min(haversine_km(lat, lon, plat, plon) for plat, plon in parks)


# ── Boliga fetcher ───────────────────────────────────────────────────────────

def fetch_listings(criteria: dict) -> list[dict]:
    """Fetch all matching listings from Boliga across municipalities."""
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Widen the API search to capture listings matching ≥2 criteria
    search_price_min = int(criteria["budget_min"] * 0.6)
    search_price_max = int(criteria["budget_max"] * 1.25)
    search_size_min = max(30, int(criteria["min_sqm"] * 0.7))

    all_results = []
    seen_ids: set[int] = set()

    for muni in MUNICIPALITIES:
        page = 1
        while True:
            params = {
                "pageSize": PAGE_SIZE,
                "page": page,
                "municipality": muni,
                "propertyType": PROPERTY_TYPES,
                "priceMin": search_price_min,
                "priceMax": search_price_max,
                "sizeMin": search_size_min,
                "sort": "date-d",
            }
            r = session.get(BOLIGA_API, params=params)
            r.raise_for_status()
            data = r.json()
            results = data["results"]

            for item in results:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    all_results.append(item)

            total_pages = data["meta"]["totalPages"]
            print(
                f"  Municipality {muni}: page {page}/{total_pages} "
                f"({len(results)} results)"
            )

            if page >= total_pages:
                break
            page += 1
            time.sleep(0.3)

        time.sleep(0.5)

    return all_results


# ── Criteria scoring ─────────────────────────────────────────────────────────

def score_criteria(row: dict, criteria: dict) -> tuple[int, list[str]]:
    """Return (count of criteria met, list of which ones)."""
    met = []

    price = row.get("pris_dkk") or 0
    if criteria["budget_min"] <= price <= criteria["budget_max"]:
        met.append("budget")

    sqm = row.get("areal_kvm") or 0
    if sqm >= criteria["min_sqm"]:
        met.append("areal")

    rooms = row.get("værelser") or 0
    if rooms >= criteria["min_bedrooms"]:
        met.append("værelser")

    bike_min = row.get("bike_min")
    if bike_min is not None and bike_min <= criteria["max_bike_min"]:
        met.append("cykelafstand")

    return len(met), met


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Danish house search via Boliga.dk")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show criteria and exit without fetching data")
    args = parser.parse_args()

    print("🏠 Danish House Search")
    print("=" * 60)

    criteria = prompt_criteria()
    print_criteria(criteria)

    if args.dry_run:
        print("\n🏁 Dry-run — no data fetched. Criteria look good? Re-run without --dry-run to search.")
        return

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 1. Fetch listings
    print("\n📡 Fetching listings from Boliga (aggregates Home, Nybolig, EDC, …)")
    raw = fetch_listings(criteria)
    print(f"\n  Total unique listings: {len(raw)}")

    # 2. Fetch parks
    print("\n🌳 Fetching park locations from OpenStreetMap …")
    parks = fetch_parks_in_area()

    # 3. Enrich listings
    print("\n🔧 Enriching listings …")
    rows = []
    for item in raw:
        lat = item.get("latitude")
        lon = item.get("longitude")

        bike_min = estimate_bike_minutes(lat, lon) if lat and lon else None
        park_km = nearest_park_km(lat, lon, parks) if lat and lon and parks else None

        row = {
            "boliga_id": item["id"],
            "adresse": item.get("street", ""),
            "postnr": item.get("zipCode"),
            "by": item.get("city", ""),
            "kommune": item.get("municipality"),
            "boligtype": PROPERTY_TYPE_NAMES.get(item.get("propertyType"), "?"),
            "pris_dkk": item.get("price"),
            "kvm_pris": item.get("squaremeterPrice"),
            "areal_kvm": item.get("size"),
            "værelser": item.get("rooms"),
            "etage": item.get("floor"),
            "byggeår": item.get("buildYear"),
            "energimærke": item.get("energyClass", ""),
            "ejerudgift_mdl": item.get("exp"),
            "nettoudgift_mdl": item.get("net"),
            "udbetaling": item.get("downPayment"),
            "kælder_kvm": item.get("basementSize"),
            "grund_kvm": item.get("lotSize"),
            "dage_til_salg": item.get("daysForSale"),
            "oprettet": item.get("createdDate", "")[:10],
            "åbent_hus": item.get("openHouse", "")[:10] if item.get("openHouse") else "",
            "tvangsauktion": item.get("isForeclosure", False),
            "cykel_min_til_rådhuspladsen": round(bike_min, 1) if bike_min else None,
            "afstand_nærmeste_park_km": round(park_km, 2) if park_km is not None else None,
            "latitude": lat,
            "longitude": lon,
            "boliga_link": f"https://www.boliga.dk/bolig/{item['id']}",
        }

        n_met, which = score_criteria({**row, "bike_min": bike_min}, criteria)
        row["kriterier_opfyldt"] = n_met
        row["hvilke_kriterier"] = ", ".join(which)

        rows.append(row)

    # Filter: keep only listings matching ≥ 2 criteria
    rows = [r for r in rows if r["kriterier_opfyldt"] >= 2]
    rows.sort(key=lambda r: (-r["kriterier_opfyldt"], r["pris_dkk"] or 0))
    print(f"  Listings matching ≥ 2 criteria: {len(rows)}")

    if not rows:
        print("\n  No listings found matching ≥ 2 criteria. Try widening the search.")
        return

    # 4. Write CSV
    csv_path = OUTPUT_DIR / f"results_{today}.csv"
    fieldnames = list(rows[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n📄 CSV saved: {csv_path}")

    # 5. Build interactive HTML report
    print("\n📊 Building interactive HTML report …")
    df = pd.DataFrame(rows)
    html_path = OUTPUT_DIR / f"results_{today}.html"
    build_html_report(df, html_path, today, criteria)
    print(f"🌐 HTML report saved: {html_path}")

    print("\nDone! ✅")


# ── HTML report builder ─────────────────────────────────────────────────────

def build_html_report(df: pd.DataFrame, path: Path, date_str: str, criteria: dict):
    """Create a self-contained interactive HTML report with Plotly."""

    df = df.copy()

    # Format price for display
    df["pris_mio"] = df["pris_dkk"] / 1_000_000
    df["hover_text"] = (
        df["adresse"] + ", " + df["postnr"].astype(str) + " " + df["by"]
        + "<br>Pris: " + df["pris_dkk"].apply(lambda x: f"{x:,.0f}".replace(",", ".") if pd.notna(x) else "?") + " DKK"
        + "<br>Areal: " + df["areal_kvm"].astype(str) + " m²"
        + "<br>Værelser: " + df["værelser"].astype(str)
        + "<br>Ejerudgift: " + df["ejerudgift_mdl"].apply(lambda x: f"{x:,.0f}".replace(",", ".") if pd.notna(x) else "?") + " kr/md"
        + "<br>Cykel til Rådhuspladsen: " + df["cykel_min_til_rådhuspladsen"].apply(lambda x: f"{x:.0f} min" if pd.notna(x) else "?")
        + "<br>Nærmeste park: " + df["afstand_nærmeste_park_km"].apply(lambda x: f"{x:.1f} km" if pd.notna(x) else "?")
        + "<br>Kriterier opfyldt: " + df["kriterier_opfyldt"].astype(str) + "/4"
        + " (" + df["hvilke_kriterier"] + ")"
    )

    budget_min_mio = criteria["budget_min"] / 1_000_000
    budget_max_mio = criteria["budget_max"] / 1_000_000

    # ── Map (scatter_mapbox with open-street-map — no external tile JS needed)
    warnings.filterwarnings("ignore", message=".*deprecated.*", category=DeprecationWarning)
    fig_map = px.scatter_mapbox(
        df,
        lat="latitude",
        lon="longitude",
        color="kriterier_opfyldt",
        size="areal_kvm",
        hover_name="adresse",
        custom_data=["hover_text", "boliga_link"],
        color_continuous_scale=["#e74c3c", "#f0ad4e", "#5bc0de", "#27ae60"],
        range_color=[1, 4],
        size_max=18,
        zoom=11,
        center={"lat": TARGET_LAT, "lon": TARGET_LON},
        mapbox_style="carto-positron",
        title="Boliger til salg — kort",
    )
    fig_map.update_traces(
        hovertemplate="%{customdata[0]}<extra></extra>",
    )
    fig_map.add_trace(go.Scattermapbox(
        lat=[TARGET_LAT], lon=[TARGET_LON],
        mode="markers+text",
        marker=dict(size=16, color="red"),
        text=["Rådhuspladsen"],
        textposition="top center",
        name="Rådhuspladsen",
        hoverinfo="text",
    ))
    fig_map.update_layout(height=600, margin=dict(l=0, r=0, t=40, b=0))

    # ── Price vs. size scatter
    fig_scatter = px.scatter(
        df,
        x="areal_kvm",
        y="pris_mio",
        color="kriterier_opfyldt",
        size="værelser",
        hover_name="adresse",
        custom_data=["hover_text", "boliga_link"],
        color_continuous_scale=["#e74c3c", "#f0ad4e", "#5bc0de", "#27ae60"],
        range_color=[1, 4],
        labels={"areal_kvm": "Areal (m²)", "pris_mio": "Pris (mio DKK)", "kriterier_opfyldt": "Kriterier"},
        title="Pris vs. areal",
    )
    fig_scatter.update_traces(hovertemplate="%{customdata[0]}<extra></extra>")
    fig_scatter.add_hrect(
        y0=budget_min_mio, y1=budget_max_mio, fillcolor="green", opacity=0.07,
        annotation_text=f"Budget {budget_min_mio:.1f}–{budget_max_mio:.1f} mio",
        annotation_position="top left",
    )
    fig_scatter.add_vrect(
        x0=criteria["min_sqm"], x1=df["areal_kvm"].max() * 1.05,
        fillcolor="blue", opacity=0.04,
        annotation_text=f">{criteria['min_sqm']} m²",
        annotation_position="top right",
    )

    # ── Price vs. bike time
    fig_bike = px.scatter(
        df,
        x="cykel_min_til_rådhuspladsen",
        y="pris_mio",
        color="boligtype",
        hover_name="adresse",
        custom_data=["hover_text", "boliga_link"],
        labels={"cykel_min_til_rådhuspladsen": "Cykel til Rådhuspladsen (min)", "pris_mio": "Pris (mio DKK)"},
        title="Pris vs. cykelafstand til Rådhuspladsen",
    )
    fig_bike.update_traces(hovertemplate="%{customdata[0]}<extra></extra>")
    fig_bike.add_vrect(
        x0=0, x1=criteria["max_bike_min"], fillcolor="green", opacity=0.06,
        annotation_text=f"<{criteria['max_bike_min']} min",
        annotation_position="top left",
    )

    # ── Compose HTML — first chart includes plotly.js via CDN, rest reuse it
    map_html = fig_map.to_html(full_html=False, include_plotlyjs="cdn")
    scatter_html = fig_scatter.to_html(full_html=False, include_plotlyjs=False)
    bike_html = fig_bike.to_html(full_html=False, include_plotlyjs=False)

    # Build sortable table
    display_cols = [
        ("adresse", "Adresse"),
        ("postnr", "Postnr"),
        ("by", "By"),
        ("boligtype", "Type"),
        ("pris_dkk", "Pris (DKK)"),
        ("areal_kvm", "m²"),
        ("værelser", "Værelser"),
        ("etage", "Etage"),
        ("ejerudgift_mdl", "Ejerudgift/md"),
        ("nettoudgift_mdl", "Nettoudgift/md"),
        ("udbetaling", "Udbetaling"),
        ("cykel_min_til_rådhuspladsen", "Cykel (min)"),
        ("afstand_nærmeste_park_km", "Park (km)"),
        ("energimærke", "Energi"),
        ("byggeår", "Byggeår"),
        ("dage_til_salg", "Dage til salg"),
        ("oprettet", "Oprettet"),
        ("kriterier_opfyldt", "Kriterier"),
        ("hvilke_kriterier", "Hvilke"),
        ("boliga_link", "Link"),
    ]

    criteria_col_idx = next(i for i, (col, _) in enumerate(display_cols) if col == "kriterier_opfyldt")

    table_header = "".join(
        f'<th onclick="sortTable({i})">{label} ⇅</th>'
        for i, (_, label) in enumerate(display_cols)
    )
    table_rows = ""
    for _, row in df.iterrows():
        cells = ""
        for col, _ in display_cols:
            val = row[col]
            if col == "boliga_link":
                cells += f'<td><a href="{val}" target="_blank">Se bolig</a></td>'
            elif col == "pris_dkk" and pd.notna(val):
                cells += f'<td data-sort="{val}">{val:,.0f}</td>'.replace(",", ".")
            elif col in ("ejerudgift_mdl", "nettoudgift_mdl", "udbetaling") and pd.notna(val):
                cells += f'<td data-sort="{val}">{val:,.0f}</td>'.replace(",", ".")
            elif pd.isna(val):
                cells += '<td data-sort="">–</td>'
            else:
                cells += f"<td>{val}</td>"
        criteria_count = row["kriterier_opfyldt"]
        row_class = "match4" if criteria_count == 4 else "match3" if criteria_count == 3 else ""
        table_rows += f'<tr class="{row_class}">{cells}</tr>\n'

    criteria_summary = (
        f'<span class="criteria-box">Budget {budget_min_mio:.1f}–{budget_max_mio:.1f} mio DKK</span>'
        f'<span class="criteria-box">&ge; {criteria["min_bedrooms"]} værelser</span>'
        f'<span class="criteria-box">&gt; {criteria["min_sqm"]} m²</span>'
        f'<span class="criteria-box">&lt; {criteria["max_bike_min"]} min cykel til Rådhuspladsen</span>'
    )

    html = f"""<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boligsøgning — {date_str}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         margin: 0; padding: 20px; background: #f5f5f5; color: #333; }}
  h1 {{ color: #2c3e50; }}
  h2 {{ color: #34495e; margin-top: 2rem; }}
  .summary {{ background: white; padding: 1.2rem; border-radius: 8px; margin-bottom: 1.5rem;
              box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .summary span {{ display: inline-block; margin-right: 2rem; }}
  .summary b {{ color: #2c3e50; }}
  .chart-container {{ background: white; padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem;
                      box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; background: white;
           border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th {{ background: #2c3e50; color: white; padding: 10px 8px; cursor: pointer;
       white-space: nowrap; user-select: none; }}
  th:hover {{ background: #34495e; }}
  td {{ padding: 8px; border-bottom: 1px solid #eee; }}
  tr:hover {{ background: #f0f7ff; }}
  tr.match4 {{ background: #d4edda; }}
  tr.match3 {{ background: #e8f4fd; }}
  a {{ color: #3498db; }}
  .criteria-box {{ display: inline-block; background: #ecf0f1; padding: 0.3rem 0.7rem;
                   border-radius: 4px; margin: 0.2rem; font-size: 0.9rem; }}
  .filter-bar {{ margin: 1rem 0; padding: 1rem; background: white; border-radius: 8px;
                 box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .filter-bar label {{ margin-right: 1rem; }}
  .filter-bar input, .filter-bar select {{ padding: 4px 8px; border: 1px solid #ddd; border-radius: 4px; }}
</style>
</head>
<body>

<h1>🏠 Boligsøgning København — {date_str[:4]}-{date_str[4:6]}-{date_str[6:]}</h1>

<div class="summary">
  <span><b>{len(df)}</b> boliger fundet</span>
  <span>Kriterier: {criteria_summary}</span>
</div>

<h2>Kort</h2>
<div class="chart-container">{map_html}</div>

<h2>Pris vs. areal</h2>
<div class="chart-container">{scatter_html}</div>

<h2>Pris vs. cykelafstand</h2>
<div class="chart-container">{bike_html}</div>

<h2>Alle boliger</h2>
<div class="filter-bar">
  <label>Søg: <input type="text" id="tableSearch" onkeyup="filterTable()" placeholder="adresse, by …"></label>
  <label>Min kriterier:
    <select id="minCriteria" onchange="filterTable()">
      <option value="2">≥ 2</option>
      <option value="3">≥ 3</option>
      <option value="4">= 4</option>
    </select>
  </label>
</div>

<table id="listingsTable">
<thead><tr>{table_header}</tr></thead>
<tbody>
{table_rows}
</tbody>
</table>

<p style="margin-top:2rem; color:#999; font-size:0.8rem;">
  Data fra <a href="https://www.boliga.dk">Boliga.dk</a> (aggregerer Home, Nybolig, EDC m.fl.).
  Cykelafstand er estimeret (fugleflugt × 1,35 / 16 km/t).
  Parkafstand er baseret på OpenStreetMap-data.
  Genereret {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}.
</p>

<script>
let sortDir = {{}};
function sortTable(colIdx) {{
  const table = document.getElementById("listingsTable");
  const tbody = table.querySelector("tbody");
  const rows = Array.from(tbody.rows);
  const dir = sortDir[colIdx] === "asc" ? "desc" : "asc";
  sortDir[colIdx] = dir;
  rows.sort((a, b) => {{
    let va = a.cells[colIdx].dataset.sort !== undefined && a.cells[colIdx].dataset.sort !== ""
             ? a.cells[colIdx].dataset.sort : a.cells[colIdx].textContent.trim();
    let vb = b.cells[colIdx].dataset.sort !== undefined && b.cells[colIdx].dataset.sort !== ""
             ? b.cells[colIdx].dataset.sort : b.cells[colIdx].textContent.trim();
    const na = parseFloat(va.replace(/\\./g, "").replace(",", "."));
    const nb = parseFloat(vb.replace(/\\./g, "").replace(",", "."));
    if (!isNaN(na) && !isNaN(nb)) {{
      return dir === "asc" ? na - nb : nb - na;
    }}
    return dir === "asc" ? va.localeCompare(vb, "da") : vb.localeCompare(va, "da");
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

function filterTable() {{
  const search = document.getElementById("tableSearch").value.toLowerCase();
  const minC = parseInt(document.getElementById("minCriteria").value);
  const rows = document.querySelectorAll("#listingsTable tbody tr");
  rows.forEach(row => {{
    const text = row.textContent.toLowerCase();
    const criteriaCell = row.cells[{criteria_col_idx}];
    const criteria = parseInt(criteriaCell?.textContent) || 0;
    row.style.display = (text.includes(search) && criteria >= minC) ? "" : "none";
  }});
}}
</script>

</body>
</html>"""

    path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
