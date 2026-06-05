"""
Data Processor
Combina datos de SEC EDGAR + Dataroma y genera output/data.json
con la estructura que consume el frontend.
"""
import json, os
from datetime import datetime, timezone
from collections import defaultdict

# Mapeo de nombre de issuer (fragmento) → sector
SECTOR_MAP = {
    "apple":       "Technology",    "microsoft":   "Technology",
    "alphabet":    "Comm. Services","google":      "Comm. Services",
    "meta":        "Comm. Services","facebook":    "Comm. Services",
    "amazon":      "Cons. Discret.","nvidia":      "Technology",
    "broadcom":    "Technology",    "oracle":      "Technology",
    "salesforce":  "Technology",    "adobe":       "Technology",
    "berkshire":   "Financials",    "jpmorgan":    "Financials",
    "bank of am":  "Financials",    "wells fargo": "Financials",
    "american exp":"Financials",    "visa":        "Financials",
    "mastercard":  "Financials",    "moody":       "Financials",
    "coca-cola":   "Cons. Staples", "procter":     "Cons. Staples",
    "walmart":     "Cons. Staples", "costco":      "Cons. Staples",
    "occidental":  "Energy",        "chevron":     "Energy",
    "exxon":       "Energy",        "pioneer":     "Energy",
    "unitedhealth":"Healthcare",    "johnson":     "Healthcare",
    "abbvie":      "Healthcare",    "eli lilly":   "Healthcare",
    "delta":       "Industrials",   "union pac":   "Industrials",
    "caterpillar": "Industrials",   "deere":       "Industrials",
}

def guess_sector(company: str) -> str:
    c = company.lower()
    for keyword, sector in SECTOR_MAP.items():
        if keyword in c:
            return sector
    return "Other"

# ── Consensus score ───────────────────────────────────────────────

def consensus_score(owners: int, max_owners: int, activity: str, portfolio_weight: float) -> int:
    base           = (owners / max_owners) * 60 if max_owners else 0
    activity_bonus = 20 if activity == "buy" else 0
    weight_bonus   = min(portfolio_weight * 2, 20)
    return round(base + activity_bonus + weight_bonus)

# ── Sector flows desde fondos SEC ────────────────────────────────

def compute_sector_flows(funds: list) -> list:
    """Suma buys / sells por sector entre todos los fondos."""
    flows   = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "total": 0.0})
    weights = defaultdict(float)

    for fund in funds:
        for h in fund.get("holdings", []):
            sector = guess_sector(h.get("company", ""))
            val    = h.get("value", 0)
            act    = h.get("activity", "hold")
            weights[sector] += val
            if act in ("new", "added"):
                flows[sector]["buy"]  += val
            elif act in ("reduced", "sold"):
                flows[sector]["sell"] += val
            flows[sector]["total"] += val

    grand_total = sum(weights.values()) or 1
    result = []
    for sector, w in sorted(weights.items(), key=lambda x: -x[1]):
        net   = flows[sector]["buy"] - flows[sector]["sell"]
        pct   = round(w / grand_total * 100, 1)
        trend = "buy" if net > 0 else ("sell" if net < 0 else "neutral")
        sign  = "+" if net >= 0 else ""
        result.append({
            "name":  sector,
            "pct":   pct,
            "flow":  f"{sign}{net/1e9:.1f}B",
            "trend": trend,
        })
    return result

# ── Build top moves ───────────────────────────────────────────────

def build_top_moves(funds: list, activity_types: list, top_n: int = 10) -> list:
    """Agrega los mayores movimientos (buys o sells) entre todos los fondos."""
    moves = []
    for fund in funds:
        for h in fund.get("holdings", []):
            if h.get("activity") in activity_types:
                moves.append({
                    "ticker":    h["ticker"],
                    "company":   h["company"],
                    "fund":      fund["fund"],
                    "value":     h["value"],
                    "activity":  h["activity"],
                    "delta":     h.get("delta_shares"),
                })

    moves.sort(key=lambda x: abs(x["value"]), reverse=True)
    # Deduplicar por ticker — quedar con el mayor movimiento
    seen, result = set(), []
    for m in moves:
        if m["ticker"] not in seen:
            seen.add(m["ticker"])
            result.append(m)
    return result[:top_n]

# ── Construir lista de inversores ─────────────────────────────────

def build_investors(funds: list) -> list:
    result = []
    for fund in funds:
        top3 = fund["holdings"][:3]
        result.append({
            "name":    fund["fund"],
            "aum":     f"${fund['aum_reported']/1e9:.0f}B",
            "period":  fund.get("period", ""),
            "top3":    [h["ticker"] for h in top3],
        })
    result.sort(key=lambda x: float(x["aum"].replace("$","").replace("B","")), reverse=True)
    return result

# ── Proceso principal ─────────────────────────────────────────────

def process(
    funds_dir:   str = "raw/funds",
    dataroma_path: str = "raw/grand_portfolio.json",
    output_path: str = "output/data.json",
) -> dict:

    # 1. Cargar datos de fondos SEC EDGAR
    funds = []
    if os.path.isdir(funds_dir):
        for fname in os.listdir(funds_dir):
            if fname.endswith(".json"):
                with open(os.path.join(funds_dir, fname)) as f:
                    funds.append(json.load(f))
    print(f"  Fondos cargados: {len(funds)}")

    # 2. Cargar Dataroma
    dataroma = None
    if os.path.exists(dataroma_path):
        with open(dataroma_path) as f:
            dataroma = json.load(f)
    else:
        print("  ⚠ grand_portfolio.json no encontrado — usando solo SEC EDGAR")

    # 3. Construir tabla de consenso
    # Fuente primaria: Dataroma (82 managers); fallback: contar owners por ticker en fondos SEC
    ticker_data = defaultdict(lambda: {
        "owners": 0, "activity": "hold", "portfolio_weight": 0.0,
        "company": "", "sector": "",
    })

    if dataroma:
        max_owners = max((h.get("owners_count", 0) for h in dataroma["holdings"]), default=1)
        for h in dataroma["holdings"]:
            t = h["ticker"]
            ticker_data[t]["owners"]           = h.get("owners_count", 0)
            ticker_data[t]["activity"]         = h.get("recent_activity", "hold")
            ticker_data[t]["portfolio_weight"] = h.get("portfolio_weight_pct", 0)
            ticker_data[t]["company"]          = h.get("company", t)
            ticker_data[t]["sector"]           = guess_sector(h.get("company", ""))
    else:
        # Sin Dataroma: agregar por fondos SEC
        max_owners = len(funds) or 1
        for fund in funds:
            for h in fund["holdings"][:20]:  # top 20 por fondo
                t = h["ticker"]
                ticker_data[t]["owners"]   += 1
                ticker_data[t]["company"]   = h.get("company", t)
                ticker_data[t]["sector"]    = guess_sector(h.get("company", ""))
                if h["activity"] in ("new","added"):
                    ticker_data[t]["activity"] = "buy"

    consensus = []
    for ticker, d in ticker_data.items():
        if not ticker or ticker == "Unknown":
            continue
        score = consensus_score(
            d["owners"], max_owners, d["activity"], d["portfolio_weight"]
        )
        consensus.append({
            "ticker":   ticker,
            "company":  d["company"],
            "sector":   d["sector"],
            "owners":   d["owners"],
            "activity": d["activity"],
            "score":    score,
            "thesis":   "",  # Se enriquece manualmente o con LLM
        })

    consensus.sort(key=lambda x: x["score"], reverse=True)

    # 4. Sectores
    sectors = compute_sector_flows(funds)

    # 5. Top moves
    top_buys  = build_top_moves(funds, ["new", "added"])
    top_sells = build_top_moves(funds, ["reduced", "sold"])

    # 6. Inversores
    investors = build_investors(funds)

    # 7. Periodo
    period = funds[0]["period"] if funds else dataroma.get("period", "") if dataroma else ""

    data = {
        "meta": {
            "generated_at":      datetime.now(timezone.utc).isoformat(),
            "sec_filings_period": period,
            "dataroma_managers": dataroma.get("total_managers", 0) if dataroma else 0,
            "funds_verified":    len(funds),
        },
        "quarter":            _period_to_quarter(period),
        "totalInvestors":     dataroma.get("total_managers", len(funds)) if dataroma else len(funds),
        "grandPortfolioValue": _estimate_aum(funds),
        "consensus":          consensus[:30],
        "topBuys":            top_buys,
        "topSells":           top_sells,
        "sectors":            sectors,
        "investors":          investors,
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  ✓ data.json generado: {len(consensus)} consensus, {len(sectors)} sectores")
    return data

# ── Helpers ───────────────────────────────────────────────────────

def _period_to_quarter(period: str) -> str:
    """'2026-03-31' → 'Q1 2026'"""
    try:
        from datetime import datetime
        d = datetime.strptime(period, "%Y-%m-%d")
        q = (d.month - 1) // 3 + 1
        return f"Q{q} {d.year}"
    except Exception:
        return period

def _estimate_aum(funds: list) -> str:
    total = sum(f.get("aum_reported", 0) for f in funds)
    if total > 1e12:
        return f"{total/1e12:.2f}T"
    return f"{total/1e9:.0f}B"
