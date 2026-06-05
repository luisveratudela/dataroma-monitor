"""
Dataroma Scraper
Extrae el Grand Portfolio de Dataroma usando Playwright + stealth.
Dataroma bloquea requests directos — playwright es necesario.
"""
import json, os, re
from datetime import datetime, timezone

# ── Playwright con stealth ─────────────────────────────────────────

def _get_page(url: str, selector: str = "table", wait_ms: int = 8000):
    from playwright.sync_api import sync_playwright
    try:
        from playwright_stealth import stealth_sync
        USE_STEALTH = True
    except ImportError:
        USE_STEALTH = False
        print("  ⚠ playwright-stealth no instalado — puede ser bloqueado")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        if USE_STEALTH:
            stealth_sync(page)

        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_selector(selector, timeout=wait_ms)
        html = page.inner_html(selector)
        browser.close()
        return html

# ── Parseo de tablas HTML ─────────────────────────────────────────

def _parse_table(html: str) -> list[dict]:
    """Parsea <table> HTML sin pandas — solo stdlib."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    headers, records = [], []

    for i, row in enumerate(rows):
        cells_raw = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells_raw]
        if not cells:
            continue
        if i == 0 or not headers:
            headers = [c.lower().replace(" ", "_").replace("%", "pct").replace("#", "n") for c in cells]
        else:
            records.append(dict(zip(headers, cells)))

    return records

# ── Scrapers ──────────────────────────────────────────────────────

URLS = {
    "grand":    "https://www.dataroma.com/m/g/portfolio.php",
    "buys":     "https://www.dataroma.com/m/g/portfolio_b.php?q=q",
    "sells":    "https://www.dataroma.com/m/g/portfolio_s.php?q=q",
    "managers": "https://www.dataroma.com/m/managers.php",
}

def scrape_grand_portfolio() -> list[dict]:
    print("  Scraping Grand Portfolio...")
    html = _get_page(URLS["grand"], selector="table#grid")
    rows = _parse_table(html)

    holdings = []
    for r in rows:
        # Columnas típicas: company, ticker, owners, portfolio_%
        ticker  = (r.get("ticker") or r.get("sym") or "").upper().strip()
        company = r.get("company") or r.get("stock") or ticker
        try:    owners = int(re.sub(r"\D", "", r.get("owners") or r.get("n_holders") or "0") or 0)
        except: owners = 0
        try:    pct = float(re.sub(r"[^0-9.]", "", r.get("portfolio_pct") or r.get("pct") or "0") or 0)
        except: pct = 0.0

        if ticker:
            holdings.append({
                "ticker":              ticker,
                "company":             company,
                "owners_count":        owners,
                "portfolio_weight_pct": pct,
                "recent_activity":     "hold",
            })

    print(f"  ✓ {len(holdings)} holdings en Grand Portfolio")
    return holdings

def scrape_recent_moves(kind: str = "buys") -> list[dict]:
    """kind = 'buys' | 'sells'"""
    url = URLS[kind]
    print(f"  Scraping {kind}...")
    html = _get_page(url, selector="table#grid")
    rows = _parse_table(html)

    moves = []
    for r in rows:
        ticker = (r.get("ticker") or r.get("sym") or "").upper().strip()
        if ticker:
            moves.append({
                "ticker":  ticker,
                "company": r.get("company") or r.get("stock") or ticker,
                "manager": r.get("manager") or r.get("investor") or "",
                "type":    kind[:-1],  # "buy" / "sell"
            })

    print(f"  ✓ {len(moves)} {kind}")
    return moves

def scrape_managers() -> list[dict]:
    print("  Scraping managers list...")
    html = _get_page(URLS["managers"], selector="table")
    rows = _parse_table(html)
    managers = []
    for r in rows:
        name = r.get("manager") or r.get("name") or r.get("investor") or ""
        if name:
            managers.append({"name": name, "aum": r.get("portfolio_value") or ""})
    print(f"  ✓ {len(managers)} managers")
    return managers

# ── Punto de entrada ──────────────────────────────────────────────

def scrape_all(output_dir: str = "raw") -> dict:
    os.makedirs(output_dir, exist_ok=True)

    holdings  = scrape_grand_portfolio()
    buys      = scrape_recent_moves("buys")
    sells     = scrape_recent_moves("sells")
    managers  = scrape_managers()

    # Marcar actividad reciente en holdings
    buy_tickers  = {m["ticker"] for m in buys}
    sell_tickers = {m["ticker"] for m in sells}
    for h in holdings:
        if h["ticker"] in buy_tickers:
            h["recent_activity"] = "buy"
        elif h["ticker"] in sell_tickers:
            h["recent_activity"] = "sell"

    result = {
        "period":         _current_quarter(),
        "scraped_at":     datetime.now(timezone.utc).isoformat(),
        "total_managers": len(managers),
        "managers":       managers,
        "holdings":       holdings,
        "recent_buys":    buys,
        "recent_sells":   sells,
    }

    out_path = os.path.join(output_dir, "grand_portfolio.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"  ✓ Guardado en {out_path}")
    return result

def _current_quarter() -> str:
    now = datetime.now()
    q = (now.month - 1) // 3 + 1
    return f"Q{q} {now.year}"
