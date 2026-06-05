"""
SEC EDGAR 13F Parser
Descarga 13F-HR de los fondos configurados y extrae holdings.
API pública, sin autenticación. Rate limit: 10 req/seg.
"""
import json, os, time, xml.etree.ElementTree as ET, requests

HEADERS = {"User-Agent": "DataromaMonitor contact@example.com"}  # SEC lo exige
BASE    = "https://data.sec.gov"

FUNDS = {
    "berkshire":   {"name": "Berkshire Hathaway",    "cik": "0001067983"},
    "pershing":    {"name": "Pershing Square",        "cik": "0001336528"},
    "third_point": {"name": "Third Point",            "cik": "0001040273"},
    "tiger_global":{"name": "Tiger Global",           "cik": "0001167483"},
    "bridgewater": {"name": "Bridgewater",            "cik": "0001350694"},
    "duquesne":    {"name": "Duquesne Family Office", "cik": "0001536411"},
    "appaloosa":   {"name": "Appaloosa Management",   "cik": "0001070154"},
    "gotham":      {"name": "Gotham Asset Management","cik": "0001579982"},
    "baupost":     {"name": "Baupost Group",          "cik": "0000930028"},
}

# ── HTTP helpers ──────────────────────────────────────────────────

def _get(url, as_json=True):
    time.sleep(0.12)  # respeta 10 req/seg
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json() if as_json else r.text

# ── CUSIP → Ticker via OpenFIGI (free, sin API key) ───────────────

_figi_cache = {}

def cusip_to_ticker(cusip: str) -> str:
    if cusip in _figi_cache:
        return _figi_cache[cusip]
    try:
        time.sleep(0.12)
        r = requests.post(
            "https://api.openfigi.com/v3/mapping",
            json=[{"idType": "ID_CUSIP", "idValue": cusip}],
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data = r.json()
        if data and data[0].get("data"):
            ticker = data[0]["data"][0].get("ticker", "")
            _figi_cache[cusip] = ticker
            return ticker
    except Exception:
        pass
    _figi_cache[cusip] = ""
    return ""

# ── Filings ───────────────────────────────────────────────────────

def find_13f_filings(cik: str, max_results: int = 2) -> list:
    """Devuelve los últimos N 13F-HR filings ordenados por fecha desc."""
    data = _get(f"{BASE}/submissions/CIK{cik}.json")
    recent = data.get("filings", {}).get("recent", {})

    filings = []
    for i, form in enumerate(recent.get("form", [])):
        if form in ("13F-HR", "13F-HR/A"):
            filings.append({
                "accession": recent["accessionNumber"][i].replace("-", ""),
                "date":      recent["filingDate"][i],
                "form":      form,
            })
    filings.sort(key=lambda x: x["date"], reverse=True)
    return filings[:max_results]

def get_infotable_xml(cik: str, accession: str) -> str:
    cik_int = str(int(cik))
    # SEC EDGAR requiere el formato {accession_con_guiones}-index.json
    acc_dashed = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    idx = _get(f"{BASE}/Archives/edgar/data/{cik_int}/{accession}/{acc_dashed}-index.json")

    # Buscar archivo infotable.xml
    name = None
    for item in idx.get("directory", {}).get("item", []):
        n = item.get("name", "").lower()
        if "infotable" in n and n.endswith(".xml"):
            name = item["name"]
            break
    if not name:
        # Fallback: primer .xml que no sea primary_doc
        for item in idx.get("directory", {}).get("item", []):
            n = item.get("name", "")
            if n.endswith(".xml") and "primary" not in n.lower():
                name = n
                break
    if not name:
        raise ValueError(f"infotable no encontrado: CIK {cik} / {accession}")

    return _get(f"{BASE}/Archives/edgar/data/{cik_int}/{accession}/{name}", as_json=False)

def parse_infotable(xml_text: str) -> list:
    """Parsea infotable.xml → lista de holdings."""
    xml_text = xml_text.replace('xmlns="', 'xmlns_removed="')
    root = ET.fromstring(xml_text)

    holdings = []
    for row in root.iter("infoTable"):
        def g(tag): el = row.find(tag); return el.text.strip() if el is not None and el.text else ""
        try:    value  = int(g("value")    or 0) * 1000   # miles → USD
        except: value  = 0
        try:    shares = int(g("sshPrnamt") or 0)
        except: shares = 0
        holdings.append({
            "company":  g("nameOfIssuer"),
            "cusip":    g("cusip"),
            "value":    value,
            "shares":   shares,
            "put_call": g("putCall"),
        })
    return holdings

# ── Por fondo ─────────────────────────────────────────────────────

def fetch_fund(fund_id: str, info: dict, output_dir: str) -> dict | None:
    cik  = info["cik"]
    name = info["name"]
    print(f"  → {name} (CIK {cik})")

    filings = find_13f_filings(cik, max_results=2)
    if not filings:
        print(f"    ⚠ Sin 13F-HR para {name}")
        return None

    latest = filings[0]
    prev   = filings[1] if len(filings) > 1 else None

    xml_curr = get_infotable_xml(cik, latest["accession"])
    curr_raw = parse_infotable(xml_curr)

    # Mapa CUSIP → shares anterior para calcular delta
    prev_shares = {}
    if prev:
        try:
            xml_prev = get_infotable_xml(cik, prev["accession"])
            for h in parse_infotable(xml_prev):
                prev_shares[h["cusip"]] = h["shares"]
        except Exception as e:
            print(f"    ⚠ No se pudo obtener trimestre anterior: {e}")

    total_value = sum(h["value"] for h in curr_raw)
    holdings = []

    for h in curr_raw:
        cusip  = h["cusip"]
        ticker = cusip_to_ticker(cusip) or h["company"][:8].upper()
        pct    = (h["value"] / total_value * 100) if total_value else 0

        ps = prev_shares.get(cusip)
        if ps is None:
            activity = "new"
        elif h["shares"] > ps * 1.01:
            activity = "added"
        elif h["shares"] < ps * 0.99:
            activity = "reduced"
        elif ps == 0 and h["shares"] == 0:
            activity = "sold"
        else:
            activity = "hold"

        holdings.append({
            "ticker":        ticker,
            "cusip":         cusip,
            "company":       h["company"],
            "value":         h["value"],
            "shares":        h["shares"],
            "portfolio_pct": round(pct, 2),
            "prev_shares":   ps,
            "delta_shares":  h["shares"] - ps if ps is not None else None,
            "activity":      activity,
            "put_call":      h["put_call"],
        })

    holdings.sort(key=lambda x: x["value"], reverse=True)

    result = {
        "fund":         name,
        "fund_id":      fund_id,
        "cik":          cik,
        "period":       latest["date"],
        "accession":    latest["accession"],
        "aum_reported": total_value,
        "holdings":     holdings,
    }

    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, f"{fund_id}.json"), "w") as f:
        json.dump(result, f, indent=2)

    print(f"    ✓ {len(holdings)} holdings · AUM ~${total_value/1e9:.1f}B · {latest['date']}")
    return result

# ── Punto de entrada ──────────────────────────────────────────────

def fetch_all_funds(output_dir: str = "raw/funds") -> list:
    results = []
    for fid, info in FUNDS.items():
        try:
            r = fetch_fund(fid, info, output_dir)
  
