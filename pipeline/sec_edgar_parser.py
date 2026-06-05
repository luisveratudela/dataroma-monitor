"""
SEC EDGAR 13F Parser
Descarga 13F-HR de los fondos configurados y extrae holdings.
API publica, sin autenticacion. Rate limit: 10 req/seg.
"""
import json
import os
import time
import xml.etree.ElementTree as ET
import requests

HEADERS = {"User-Agent": "DataromaMonitor contact@example.com"}
BASE = "https://data.sec.gov"

FUNDS = {
    "berkshire":    {"name": "Berkshire Hathaway",     "cik": "0001067983"},
    "pershing":     {"name": "Pershing Square",         "cik": "0001336528"},
    "third_point":  {"name": "Third Point",             "cik": "0001040273"},
    "tiger_global": {"name": "Tiger Global",            "cik": "0001167483"},
    "bridgewater":  {"name": "Bridgewater",             "cik": "0001350694"},
    "duquesne":     {"name": "Duquesne Family Office",  "cik": "0001536411"},
    "appaloosa":    {"name": "Appaloosa Management",    "cik": "0001070154"},
    "gotham":       {"name": "Gotham Asset Management", "cik": "0001579982"},
    "baupost":      {"name": "Baupost Group",           "cik": "0000930028"},
}


def _get(url, as_json=True):
    time.sleep(0.12)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if as_json:
        return r.json()
    return r.text


# CUSIP -> Ticker via OpenFIGI (gratis, sin API key)
_figi_cache = {}

def cusip_to_ticker(cusip):
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


def find_13f_filings(cik, max_results=2):
    """Devuelve los ultimos N filings 13F-HR ordenados por fecha desc."""
    data = _get("{}/submissions/CIK{}.json".format(BASE, cik))
    recent = data.get("filings", {}).get("recent", {})

    filings = []
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])

    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            filings.append({
                "accession": accessions[i].replace("-", ""),
                "date": dates[i],
                "form": form,
            })

    filings.sort(key=lambda x: x["date"], reverse=True)
    return filings[:max_results]


def get_infotable_xml(cik, accession):
    cik_int = str(int(cik))
    # SEC EDGAR requiere el formato {accession_con_guiones}-index.json
    acc_dashed = "{}-{}-{}".format(accession[:10], accession[10:12], accession[12:])
    url = "{}/Archives/edgar/data/{}/{}/{}-index.json".format(BASE, cik_int, accession, acc_dashed)
    idx = _get(url)

    # Buscar archivo infotable.xml
    name = None
    for item in idx.get("directory", {}).get("item", []):
        n = item.get("name", "").lower()
        if "infotable" in n and n.endswith(".xml"):
            name = item["name"]
            break

    if name is None:
        for item in idx.get("directory", {}).get("item", []):
            n = item.get("name", "")
            if n.endswith(".xml") and "primary" not in n.lower():
                name = n
                break

    if name is None:
        raise ValueError("infotable no encontrado: CIK {} / {}".format(cik, accession))

    file_url = "{}/Archives/edgar/data/{}/{}/{}".format(BASE, cik_int, accession, name)
    return _get(file_url, as_json=False)


def parse_infotable(xml_text):
    """Parsea infotable.xml -> lista de holdings."""
    xml_text = xml_text.replace('xmlns="', 'xmlns_removed="')
    root = ET.fromstring(xml_text)

    holdings = []
    for row in root.iter("infoTable"):

        def get_text(tag):
            el = row.find(tag)
            if el is not None and el.text:
                return el.text.strip()
            return ""

        try:
            value = int(get_text("value") or 0) * 1000
        except ValueError:
            value = 0

        try:
            shares = int(get_text("sshPrnamt") or 0)
        except ValueError:
            shares = 0

        holdings.append({
            "company":  get_text("nameOfIssuer"),
            "cusip":    get_text("cusip"),
            "value":    value,
            "shares":   shares,
            "put_call": get_text("putCall"),
        })

    return holdings


def fetch_fund(fund_id, info, output_dir):
    cik = info["cik"]
    name = info["name"]
    print("  -> {} (CIK {})".format(name, cik))

    filings = find_13f_filings(cik, max_results=2)
    if not filings:
        print("    Sin 13F-HR para {}".format(name))
        return None

    latest = filings[0]
    prev = filings[1] if len(filings) > 1 else None

    xml_curr = get_infotable_xml(cik, latest["accession"])
    curr_raw = parse_infotable(xml_curr)

    # Shares del trimestre anterior para calcular delta
    prev_shares = {}
    if prev is not None:
        try:
            xml_prev = get_infotable_xml(cik, prev["accession"])
            for h in parse_infotable(xml_prev):
                prev_shares[h["cusip"]] = h["shares"]
        except Exception as e:
            print("    No se pudo obtener trimestre anterior: {}".format(e))

    total_value = sum(h["value"] for h in curr_raw)
    holdings = []

    for h in curr_raw:
        cusip = h["cusip"]
        ticker = cusip_to_ticker(cusip) or h["company"][:8].upper()

        if total_value > 0:
            pct = h["value"] / total_value * 100
        else:
            pct = 0.0

        ps = prev_shares.get(cusip)
        if ps is None:
            activity = "new"
        elif h["shares"] > ps * 1.01:
            activity = "added"
        elif h["shares"] < ps * 0.99:
            activity = "reduced"
        else:
            activity = "hold"

        delta = h["shares"] - ps if ps is not None else None

        holdings.append({
            "ticker":        ticker,
            "cusip":         cusip,
            "company":       h["company"],
            "value":         h["value"],
            "shares":        h["shares"],
            "portfolio_pct": round(pct, 2),
            "prev_shares":   ps,
            "delta_shares":  delta,
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
    out_path = os.path.join(output_dir, "{}.json".format(fund_id))
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print("    {} holdings · AUM ~${:.1f}B · {}".format(
        len(holdings), total_value / 1e9, latest["date"]
    ))
    return result


def fetch_all_funds(output_dir="raw/funds"):
    results = []
    for fid, info in FUNDS.items():
        try:
            r = fetch_fund(fid, info, output_dir)
            if r is not None:
                results.append(r)
        except Exception as e:
            print("  Error en {}: {}".format(info["name"], e))
    return results
