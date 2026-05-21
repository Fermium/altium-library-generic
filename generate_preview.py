#!/usr/bin/env python3
"""
Fetch SVG previews for every SchLib symbol and generate:
  - docs/svgs/<name>.svg        individual SVG files
  - docs/index.html             searchable card-grid browser
  - README.md                   updated between comment markers

File discovery: all *.SchLib files under symbols/ (filesystem scan).
Human-readable names and categories: loaded from symbols.yaml when present;
files not listed in the YAML fall back to the raw symbol ID and a
filesystem-derived category.
"""

import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
import concurrent.futures
from collections import defaultdict
from pathlib import Path

import requests
import yaml

ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

API_BASE = os.environ["ALTIUM_MONKEY_API_URL"]
SYMBOLS_DIR = Path("symbols")
DOCS_DIR = Path("docs")
SVG_DIR = DOCS_DIR / "svgs"
MANIFEST = Path("symbols.yaml")

# Display order for known categories; unknown ones are appended alphabetically.
CAT_ORDER = [
    "Passive", "Semiconductor", "Protection", "Crystal",
    "Electromechanical", "Connector", "Test Point", "Sensor",
    "Mechanical", "Pin-Specific", "Header",
    "Generic", "Headers",  # fallback names for unlisted files
]


# ---------------------------------------------------------------------------
# SVG post-processing
# ---------------------------------------------------------------------------

def _pts(attr: str):
    """Yield (x, y) floats from a 'x1,y1 x2,y2 ...' points string."""
    for token in re.split(r"[\s,]+", attr.strip()):
        pass  # handled below
    pairs = re.findall(r"(-?[\d.]+)\s*,\s*(-?[\d.]+)", attr)
    for x, y in pairs:
        yield float(x), float(y)

def _sw(el: ET.Element) -> float:
    raw = el.get("stroke-width", "1").rstrip("px").strip()
    try:
        return float(raw) / 2
    except ValueError:
        return 0.5

def tighten_viewbox(svg_text: str, padding: float = 6.0) -> str:
    """Remove the white background rect and fit viewBox tightly to content."""
    decl, _, body = svg_text.partition("\n")
    root = ET.fromstring(body if decl.startswith("<?") else svg_text)
    svgns = "http://www.w3.org/2000/svg"

    # Remove BackgroundGroup
    for parent in root.iter():
        for child in list(parent):
            if child.get("id") == "BackgroundGroup":
                parent.remove(child)

    # Compute bounding box over all geometry
    xs, ys = [], []

    def add(x, y):
        xs.append(x); ys.append(y)

    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        sw = _sw(el)
        if tag == "line":
            for attr in ("x1", "x2"):
                add(float(el.get(attr, 0)) + sw, float(el.get(attr.replace("x", "y"), 0)) + sw)
                add(float(el.get(attr, 0)) - sw, float(el.get(attr.replace("x", "y"), 0)) - sw)
        elif tag == "polygon":
            for x, y in _pts(el.get("points", "")):
                add(x, y)
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width", 0)), float(el.get("height", 0))
            add(x, y); add(x + w, y + h)
        elif tag == "ellipse":
            cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
            rx, ry = float(el.get("rx", 0)), float(el.get("ry", 0))
            add(cx - rx, cy - ry); add(cx + rx, cy + ry)
        elif tag == "text":
            add(float(el.get("x", 0)), float(el.get("y", 0)))

    if not xs:
        return svg_text

    vx = min(xs) - padding
    vy = min(ys) - padding
    vw = max(xs) - min(xs) + 2 * padding
    vh = max(ys) - min(ys) + 2 * padding

    root.set("viewBox", f"{vx:.2f} {vy:.2f} {vw:.2f} {vh:.2f}")
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        + ET.tostring(root, encoding="unicode")
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def fetch_svgs(schlib_path: Path) -> dict[str, str]:
    """Return {symbol_name: svg_string} for a .SchLib file."""
    with open(schlib_path, "rb") as fh:
        resp = requests.post(
            f"{API_BASE}/schlib/svg",
            files={"file": (schlib_path.name, fh, "application/octet-stream")},
            timeout=60,
        )
    resp.raise_for_status()

    svgs: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for entry in zf.namelist():
            if entry.endswith(".svg"):
                svgs[Path(entry).stem] = zf.read(entry).decode("utf-8")
    return svgs


# ---------------------------------------------------------------------------
# Manifest (symbols.yaml)
# ---------------------------------------------------------------------------

def load_manifest() -> dict[str, dict]:
    """Return {posix_path: {name, category}} from symbols.yaml, or {} if missing."""
    if not MANIFEST.exists():
        return {}
    with open(MANIFEST) as f:
        data = yaml.safe_load(f)
    return {
        Path(e["file"]).as_posix(): {"name": e["name"], "category": e["category"]}
        for e in data.get("symbols", [])
    }


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_schlibs() -> list[Path]:
    return sorted(p for p in SYMBOLS_DIR.rglob("*") if p.suffix.lower() == ".schlib")


def fallback_category(path: Path) -> str:
    parts = [p.upper() for p in path.parts]
    if "HEADERS" in parts:
        return "Header"
    if "PIN_SPECIFIC" in parts:
        return "Pin-Specific"
    return "Generic"


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Altium Generic Library — Component Browser</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3250;
    --accent: #4f8ef7;
    --accent2: #7c5cfc;
    --text: #e8eaf6;
    --muted: #8b92b8;
    --green: #43d9a2;
    --radius: 10px;
  }}

  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }}

  header {{
    background: linear-gradient(135deg, #141726 0%, #1e2140 100%);
    border-bottom: 1px solid var(--border);
    padding: 2rem 2.5rem 1.5rem;
  }}
  header h1 {{
    font-size: 1.6rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  header p {{ color: var(--muted); margin-top: 0.4rem; font-size: 0.9rem; }}

  .stats {{
    display: flex;
    gap: 1.5rem;
    margin-top: 1.2rem;
    flex-wrap: wrap;
  }}
  .stat {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.35rem 0.85rem;
    font-size: 0.8rem;
    color: var(--muted);
  }}
  .stat strong {{ color: var(--accent); }}

  .controls {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 0.9rem 2.5rem;
    display: flex;
    gap: 0.8rem;
    align-items: center;
    flex-wrap: wrap;
  }}

  .search-wrap {{
    position: relative;
    flex: 1;
    min-width: 200px;
    max-width: 380px;
  }}
  .search-wrap svg {{
    position: absolute;
    left: 0.75rem;
    top: 50%;
    transform: translateY(-50%);
    opacity: 0.4;
  }}
  #search {{
    width: 100%;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 0.5rem 0.75rem 0.5rem 2.2rem;
    font-size: 0.9rem;
    outline: none;
    transition: border-color .15s;
  }}
  #search:focus {{ border-color: var(--accent); }}

  .filters {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
  .filter-btn {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    color: var(--muted);
    padding: 0.35rem 1rem;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all .15s;
  }}
  .filter-btn:hover {{ border-color: var(--accent); color: var(--text); }}
  .filter-btn.active {{
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }}

  main {{ padding: 1.5rem 2.5rem 3rem; }}

  .category-section {{ margin-bottom: 2.5rem; }}
  .category-section h2 {{
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
    margin-bottom: 1rem;
  }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 0.9rem;
  }}

  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.9rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    cursor: pointer;
    transition: border-color .15s, transform .15s, box-shadow .15s;
    position: relative;
    overflow: hidden;
  }}
  .card::before {{
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(135deg, transparent 60%, rgba(79,142,247,.04));
    pointer-events: none;
  }}
  .card:hover {{
    border-color: var(--accent);
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(0,0,0,.35);
  }}

  .card-preview {{
    width: 120px;
    height: 120px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #fff;
    border-radius: 6px;
    overflow: hidden;
    padding: 6px;
  }}
  .card-preview img {{
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }}

  .card-label {{
    margin-top: 0.65rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    width: 100%;
  }}

  .card-name {{
    font-size: 0.78rem;
    font-weight: 600;
    text-align: center;
    word-break: break-word;
    color: var(--text);
    line-height: 1.3;
  }}

  .card-id-row {{
    margin-top: 0.2rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    width: 100%;
  }}

  .card-id {{
    font-family: 'Cascadia Code', 'Fira Code', 'Courier New', monospace;
    font-size: 0.6rem;
    color: var(--muted);
    word-break: break-all;
    text-align: center;
  }}

  .copy-btn {{
    background: none;
    border: none;
    cursor: pointer;
    color: var(--muted);
    padding: 0;
    line-height: 1;
    flex-shrink: 0;
    transition: color .15s;
    display: flex;
    align-items: center;
  }}
  .copy-btn:hover {{ color: var(--accent); }}

  #toast {{
    position: fixed;
    bottom: 2rem;
    left: 50%;
    transform: translateX(-50%) translateY(1rem);
    background: var(--green);
    color: #0f1117;
    border-radius: 6px;
    padding: 0.5rem 1.2rem;
    font-size: 0.85rem;
    font-weight: 600;
    opacity: 0;
    pointer-events: none;
    transition: opacity .2s, transform .2s;
  }}
  #toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}

  .no-results {{
    color: var(--muted);
    text-align: center;
    padding: 3rem 0;
    font-size: 0.95rem;
    grid-column: 1 / -1;
  }}
</style>
</head>
<body>

<header>
  <h1>Altium Generic Library</h1>
  <p>Generic schematic symbols — parameter-free, ready for enrichment</p>
  <div class="stats">
    <div class="stat">Total symbols: <strong>{total}</strong></div>
    {cat_stats}
  </div>
</header>

<div class="controls">
  <div class="search-wrap">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input id="search" type="search" placeholder="Search by name or ID…" autocomplete="off"/>
  </div>
  <div class="filters">
    {filter_buttons}
  </div>
</div>

<main id="main">
{sections}
</main>

<div id="toast"></div>

<script>
const cards = Array.from(document.querySelectorAll('.card'));
const search = document.getElementById('search');
const filterBtns = document.querySelectorAll('.filter-btn');
const sections = document.querySelectorAll('.category-section');

let activeCat = 'all';

function applyFilters() {{
  const q = search.value.toLowerCase();
  let visible = 0;
  cards.forEach(card => {{
    const nameMatch = card.dataset.name.includes(q);
    const idMatch = card.dataset.id.includes(q);
    const matchQ = !q || nameMatch || idMatch;
    const matchCat = activeCat === 'all' || card.dataset.cat === activeCat;
    card.style.display = matchQ && matchCat ? '' : 'none';
    if (matchQ && matchCat) visible++;
  }});

  sections.forEach(sec => {{
    const any = Array.from(sec.querySelectorAll('.card')).some(c => c.style.display !== 'none');
    sec.style.display = any ? '' : 'none';
  }});

  document.querySelectorAll('.no-results').forEach(el => el.remove());
  if (visible === 0) {{
    const el = document.createElement('p');
    el.className = 'no-results';
    el.textContent = 'No components match your search.';
    document.getElementById('main').appendChild(el);
  }}
}}

search.addEventListener('input', applyFilters);

filterBtns.forEach(btn => {{
  btn.addEventListener('click', () => {{
    filterBtns.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeCat = btn.dataset.cat;
    applyFilters();
  }});
}});

const toast = document.getElementById('toast');
let toastTimer;

function showToast(text) {{
  toast.textContent = `Copied: ${{text}}`;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
}}

document.querySelectorAll('.copy-btn').forEach(btn => {{
  btn.addEventListener('click', e => {{
    e.stopPropagation();
    navigator.clipboard.writeText(btn.dataset.copy).then(() => showToast(btn.dataset.copy));
  }});
}});
</script>
</body>
</html>
"""

SECTION_TEMPLATE = """\
<div class="category-section" data-category="{cat}">
  <h2>{cat} <span style="color:var(--accent);font-weight:400;">({count})</span></h2>
  <div class="grid">
{cards}
  </div>
</div>
"""

COPY_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'

CARD_TEMPLATE = """\
    <div class="card" data-name="{name_lower}" data-id="{id_lower}" data-cat="{cat}">
      <div class="card-preview"><img src="{svg_rel}" alt="{name}" loading="lazy"/></div>
      <div class="card-label">
        <span class="card-name">{name}</span>
        <button class="copy-btn" data-copy="{name}" title="Copy full name">{copy_icon}</button>
      </div>
      <div class="card-id-row">
        <span class="card-id">{symbol_id}</span>
        <button class="copy-btn" data-copy="{symbol_id}" title="Copy Altium ID">{copy_icon}</button>
      </div>
    </div>"""


def ordered_categories(present: set[str]) -> list[str]:
    result = [c for c in CAT_ORDER if c in present]
    result += sorted(present - set(CAT_ORDER))
    return result


def build_html(components: list[dict]) -> str:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for c in components:
        by_cat[c["category"]].append(c)

    cats = ordered_categories(set(by_cat))

    filter_buttons = '\n    '.join(
        ['<button class="filter-btn active" data-cat="all">All</button>']
        + [f'<button class="filter-btn" data-cat="{cat}">{cat}</button>' for cat in cats]
    )

    sections_html = []
    cat_stats_parts = []
    for cat in cats:
        items = sorted(by_cat[cat], key=lambda x: x["name"])
        cat_stats_parts.append(
            f'<div class="stat">{cat}: <strong>{len(items)}</strong></div>'
        )
        cards_html = "\n".join(
            CARD_TEMPLATE.format(
                name=c["name"],
                name_lower=c["name"].lower(),
                symbol_id=c["id"],
                id_lower=c["id"].lower(),
                cat=cat,
                svg_rel=c["svg_rel"],
                copy_icon=COPY_ICON,
            )
            for c in items
        )
        sections_html.append(
            SECTION_TEMPLATE.format(cat=cat, count=len(items), cards=cards_html)
        )

    return HTML_TEMPLATE.format(
        total=len(components),
        cat_stats="".join(cat_stats_parts),
        filter_buttons=filter_buttons,
        sections="\n".join(sections_html),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_one(schlib_path: Path, manifest: dict) -> list[dict]:
    path_key = schlib_path.as_posix()
    entry = manifest.get(path_key)
    manifest_name = entry["name"] if entry else None
    manifest_cat = entry["category"] if entry else fallback_category(schlib_path)

    try:
        svgs = fetch_svgs(schlib_path)
    except Exception as exc:
        print(f"  SKIP {schlib_path.name}: {exc}")
        return []

    results = []
    total = len(svgs)
    for i, (symbol_id, svg_content) in enumerate(sorted(svgs.items()), 1):
        if manifest_name:
            display_name = manifest_name if total == 1 else f"{manifest_name} (Part {i})"
        else:
            display_name = symbol_id

        svg_file = SVG_DIR / f"{symbol_id}.svg"
        svg_file.write_text(tighten_viewbox(svg_content), encoding="utf-8")
        results.append({
            "id": symbol_id,
            "name": display_name,
            "category": manifest_cat,
            "svg_rel": svg_file.relative_to(DOCS_DIR).as_posix(),
        })
        print(f"  OK  {display_name} [{symbol_id}]")
    return results


def main() -> None:
    SVG_DIR.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    schlibs = find_schlibs()
    unlisted = [p for p in schlibs if p.as_posix() not in manifest]

    print(f"Found {len(schlibs)} SchLib files ({len(manifest)} in manifest, {len(unlisted)} unlisted) — fetching SVGs…\n")

    components: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(process_one, p, manifest): p for p in schlibs}
        for fut in concurrent.futures.as_completed(futures):
            components.extend(fut.result())

    if not components:
        raise RuntimeError("No symbols rendered — check ALTIUM_MONKEY_API_URL and API availability")

    print(f"\nRendered {len(components)} symbols total.")

    html_path = DOCS_DIR / "index.html"
    html_path.write_text(build_html(components), encoding="utf-8")
    print(f"Written {html_path}")


if __name__ == "__main__":
    main()
