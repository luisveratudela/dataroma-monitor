"""
Data Processor
Combina datos de SEC EDGAR y genera output/data.json
"""
import json
import os
from datetime import datetime, timezone
from collections import defaultdict

SECTOR_MAP = {
    "apple":        "Technology",     "microsoft":    "Technology",
    "alphabet":     "Comm. Services", "google":       "Comm. Services",
    "meta":         "Comm. Services", "facebook":     "Comm. Services",
    "amazon":       "Cons. Discret.", "nvidia":       "Technology",
    "broadcom":     "Technology",     "oracle":       "Technology",
    "salesforce":   "Technology",     "adobe":        "Technology",
    "berkshire":    "Financials",     "jpmorgan":     "Financials",
    "bank of am":   "Financials",     "bank america": "Financials",
    "wells fargo":  "Financials",     "american exp": "Financials",
    "visa":         "Financials",     "mastercard":   "Financials",
    "moodys":       "Financials",     "moody":        "Financials",
    "coca-cola":    "Cons. Staples",  "coca cola":    "Cons. Staples",
    "procter":      "Cons. Staples",  "walmart":      "Cons. Staples",
    "costco":       "Cons. Staples",  "kraft":        "Cons. Staples",
    "occidental":   "Energy",         "chevron":      "Energy",
    "exxon":        "Energy",         "pioneer":      "Energy",
    "unitedhealth": "Healthcare",     "johnson":      "Healthcare",
    "abbvie":       "Healthcare",     "eli lilly":    "Healthcare",
    "delta":        "Industrials",    "union pac":    "Industrials",
    "caterpillar":  "Industrials",    "deere":        "Industrials",
    "taiwan semi":  "Technology",     "tsmc":         "Technology",
    "taiwan":       "Technology",
}

def guess_sector(company):
    c = company.lower()
    for keyword, sector in SECTOR_MAP.items():
        if keyword in c:
            return sector
    return "Other"


def consensus_score(owners, max_owners, activity, portfolio_weight):
    base           = (owners / max_owners) * 60 if max_owners else 0
    activity_bonus = 20 if activity == "buy" else 0
    weight_bonus   = min(portfolio_weight * 2, 20)
    return round(base + activity_bonus + weight_bonus)


def compute_sector_flows(funds):
    flows   = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "total": 0.0})
    weights = defaultdict(float)

    for fund in funds:
        for h in fund.get("holdings", []):
            sector = guess_sector(h.get("company", ""))
            val    = h.get("value", 0)
            act    = h.get("activity", "hold")
            weights[sector] += val
            if act in ("new", "added"):
                flows[sector]["buy"] += val
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
            "flow":  "{}{:.1f}B".format(sign, net / 1e9),
            "trend": trend,
        })
    return result


def build_top_moves(funds, activity_types, top_n=10):
    moves = []
    for fund in funds:
        for h in fund.get("holdings", []):
            if h.get("activity") in activity_types:
                moves.append({
                    "ticker":   h["ticker"],
                    "company":  h["company"],
                    "fund":     fund["fund"],
                    "value":    h["value"],
                    "activity": h["activity"],
                })
    moves.sort(key=lambda x: abs(x["value"]), reverse=True)
    seen, result = set(), []
    for m in moves:
        if m["ticker"] not in seen:
            seen.add(m["ticker"])
            result.append(m)
    return result[:top_n]


def build_investors(funds):
    result = []
    for fund in funds:
        top3 = fund["holdings"][:3]
        aum  = fund.get("aum_reported", 0)
        result.append({
            "name":   fund["fund"],
            "aum":    "${:.0f}B".format(aum / 1e9),
            "period": fund.get("period", ""),
            "top3":   [h["ticker"] for h in top3],
        })
    result.sort(key=lambda x: float(x["aum"].replace("$", "").replace("B", "") or 0), reverse=True)
    return result


def _filing_date_to_report_quarter(filing_date):
    """
    Infiere el periodo reportado desde la fecha de filing.
    13F se presenta ~45 dias despues del fin de trimestre:
      Feb  -> Q4 del anio anterior
      May  -> Q1
      Aug  -> Q2
      Nov  -> Q3
    """
    try:
        d = datetime.strptime(filing_date, "%Y-%m-%d")
    except Exception:
        return filing_date

    m = d.month
    if m <= 2:
        return "Q4 {}".format(d.year - 1)
    elif m <= 5:
        return "Q1 {}".format(d.year)
    elif m <= 8:
        return "Q2 {}".format(d.year)
    else:
        return "Q3 {}".format(d.year)


def _estimate_aum(funds):
    total = sum(f.get("aum_reported", 0) for f in funds)
    if total >= 1e12:
        return "{:.2f}T".format(total / 1e12)
    return "{:.0f}B".format(total / 1e9)


def process(
    funds_dir="raw/funds",
    dataroma_path="raw/grand_portfolio.json",
    output_path="output/data.json",
):
    # 1. Cargar fondos
    funds = []
    if os.path.isdir(funds_dir):
        for fname in sorted(os.listdir(funds_dir)):
            if fname.endswith(".json"):
                with open(os.path.join(funds_dir, fname)) as f:
                    funds.append(json.load(f))
    print("  Fondos cargados: {}".format(len(funds)))

    # 2. Cargar Dataroma (opcional)
    dataroma = None
    if os.path.exists(dataroma_path):
        with open(dataroma_path) as f:
            dataroma = json.load(f)
    else:
        print("  grand_portfolio.json no encontrado -- usando solo SEC EDGAR")

    # 3. Consenso: agregar owners por ticker entre fondos SEC
    ticker_data = defaultdict(lambda: {
        "owners": 0, "portfolio_weight": 0.0,
        "company": "", "sector": "",
        "votes_buy": 0, "votes_sell": 0, "votes_hold": 0,
    })

    for fund in funds:
        seen_in_fund = set()
        for h in fund.get("holdings", []):
            t = h["ticker"]
            if t in seen_in_fund:
                continue
            seen_in_fund.add(t)
            ticker_data[t]["owners"]          += 1
            ticker_data[t]["company"]          = h.get("company", t)
            ticker_data[t]["sector"]           = guess_sector(h.get("company", ""))
            ticker_data[t]["portfolio_weight"] += h.get("portfolio_pct", 0)
            act = h.get("activity", "hold")
            if act in ("new", "added"):
                ticker_data[t]["votes_buy"]  += 1
            elif act in ("reduced", "sold"):
                ticker_data[t]["votes_sell"] += 1
            else:
                ticker_data[t]["votes_hold"] += 1

    # Actividad por mayoria de votos
    for d in ticker_data.values():
        buys  = d.pop("votes_buy")
        sells = d.pop("votes_sell")
        holds = d.pop("votes_hold")
        if buys > sells and buys >= holds:
            d["activity"] = "buy"
        elif sells > buys and sells >= holds:
            d["activity"] = "sell"
        else:
            d["activity"] = "hold"

    max_owners = max((d["owners"] for d in ticker_data.values()), default=1)

    consensus = []
    for ticker, d in ticker_data.items():
        if not ticker:
            continue
        score = consensus_score(d["owners"], max_owners, d["activity"], d["portfolio_weight"])
        consensus.append({
            "ticker":   ticker,
            "company":  d["company"],
            "sector":   d["sector"],
            "owners":   d["owners"],
            "activity": d["activity"],
            "score":    score,
            "thesis":   "",
        })
    consensus.sort(key=lambda x: x["score"], reverse=True)

    # 4. Sectores, moves, inversores
    sectors   = compute_sector_flows(funds)
    top_buys  = build_top_moves(funds, ["new", "added"])
    top_sells = build_top_moves(funds, ["reduced", "sold"])
    investors = build_investors(funds)

    # 5. Periodo: usar fecha del fondo mas reciente
    latest_period = max((f.get("period", "") for f in funds), default="") if funds else ""
    quarter = _filing_date_to_report_quarter(latest_period)

    data = {
        "meta": {
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "sec_filings_period": latest_period,
            "dataroma_managers":  dataroma.get("total_managers", 0) if dataroma else 0,
            "funds_verified":     len(funds),
        },
        "quarter":            quarter,
        "totalInvestors":     len(funds),
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

    print("  data.json: {} consensus | {} sectores | quarter: {}".format(
        len(consensus), len(sectors), quarter))
    return data
