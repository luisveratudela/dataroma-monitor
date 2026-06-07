"""
SEC EDGAR 13F Parser
Descarga 13F-HR de los fondos configurados y extrae holdings.
API publica, sin autenticacion. Rate limit: 10 req/seg.
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
import requests

SUBMISSIONS_BASE = "https://data.sec.gov"
ARCHIVES_BASE    = "https://www.sec.gov"
HEADERS = {"User-Agent": "DataromaMonitor contact@example.com"}

FUNDS = {
    "berkshire":    {"name": "Berkshire Hathaway",     "cik": "0001067983"},
    "pershing":     {"name": "Pershing Square",         "cik": "0001336528"},
    "third_point":  {"name": "Third Point",             "cik": "0001040273"},
    "tiger_global": {"name": "Tiger Global",            "cik": "0001167483"},
    "bridgewater":  {"name": "Bridgewater",             "cik": "0001350694"},
    "duquesne":     {"name": "Duquesne Family Office",  "cik": "0001536411"},
    "baupost":      {"name": "Baupost Group",           "cik": "0001061768"},
    "gotham":       {"name": "Gotham Asset Management", "cik": "0001510387"},
}


def _get(url, as_json=True, raise_on_error=True):
    time.sleep(0.12)
    r = requests.get(url, headers=HEADERS, timeout=30)
    if raise_on_error:
        r.raise_for_status()
    elif not r.ok:
        return None
    if as_json:
        return r.json()
    return r.text


# ---- CUSIP -> Ticker via OpenFIGI batch ----

_figi_cache = {}

def cusip_to_tickers_batch(cusips):
    unique = [c for c in dict.fromkeys(cusips) if c and c not in _figi_cache]
    for i in range(0, len(unique), 10):
        batch = unique[i:i+10]
        try:
            time.sleep(2.5)
            r = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": c} for c in batch],
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if not r.content:
                raise ValueError("OpenFIGI devolvio respuesta vacia (rate limit probable)")
            data = r.json()
            for j, cusip in enumerate(batch):
                if j < len(data) and data[j].get("data"):
                    _figi_cache[cusip] = data[j]["data"][0].get("ticker", "")
                else:
                    _figi_cache[cusip] = ""
        except Exception as e:
            print("    [OpenFIGI error: {}]".format(e))
            for c in batch:
                _figi_cache[c] = ""
    return {c: _figi_cache.get(c, "") for c in cusips}


# ---- Filings ----

def _extract_filings_from_page(recent):
    filings = []
    forms      = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates      = recent.get("filingDate", [])
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            filings.append({
                "accession": accessions[i].replace("-", ""),
                "date":      dates[i],
                "form":      form,
            })
    return filings


def find_13f_filings(cik, max_results=2):
    url = "{}/submissions/CIK{}.json".format(SUBMISSIONS_BASE, cik)
    data = _get(url)
    api_name = data.get("name", "unknown")
    print("    API name: {}".format(api_name))
    recent = data.get("filings", {}).get("recent", {})
    filings = _extract_filings_from_page(recent)
    if not filings:
        older_pages = data.get("filings", {}).get("files", [])
        print("    No 13F en recent, buscando en {} paginas antiguas...".format(len(older_pages)))
        for page_info in older_pages[:5]:
            try:
                page_url = "{}/submissions/{}".format(SUBMISSIONS_BASE, page_info["name"])
                page_data = _get(page_url)
                page_filings = _extract_filings_from_page(page_data)
                filings.extend(page_filings)
                if filings:
                    print("    Encontrados en pagina anterior: {}".format(page_info["name"]))
                    break
            except Exception as e:
                print("    Error en pagina anterior: {}".format(e))
    filings.sort(key=lambda x: x["date"], reverse=True)
    return filings[:max_results]


# ---- XML / SGML extraction ----

def _has_infotable(text):
    return ('infoTable>' in text or 'informationTable>' in text or
            'informationtable>' in text.lower())


def _extract_xml_from_sgml(text):
    m = re.search(r'<XML>(.*?)</XML>', text, re.DOTALL | re.IGNORECASE)
    if m:
        xml_content = m.group(1).strip()
        if _has_infotable(xml_content):
            return xml_content
    if _has_infotable(text):
        start = text.find('<?xml')
        if start < 0:
            start = text.find('<informationTable')
        if start < 0:
            start = text.lower().find('<informationtable')
        if start < 0:
            start = text.find('<infoTable')
        if start >= 0:
            return text[start:]
    return None


def get_infotable_xml(cik, accession):
    cik_int    = str(int(cik))
    acc_dashed = "{}-{}-{}".format(accession[:10], accession[10:12], accession[12:])
    base_path  = "{}/Archives/edgar/data/{}/{}".format(ARCHIVES_BASE, cik_int, accession)

    for fname in ("infotable.xml", "form13fInfoTable.xml", "13F_InfoTable.xml",
                  "13f_InfoTable.xml", "informationtable.xml"):
        result = _get("{}/{}".format(base_path, fname), as_json=False, raise_on_error=False)
        if result and _has_infotable(result):
            print("    [XML: {}]".format(fname))
            return result

    try:
        idx_url = "https://data.sec.gov/Archives/edgar/data/{}/{}/{}-index.json".format(
            cik_int, accession, acc_dashed)
        idx = _get(idx_url)
        name = _find_file_in_index(idx, extensions=(".xml", ".txt", ".htm"))
        if name:
            result = _get("{}/{}".format(base_path, name), as_json=False)
            if result:
                if _has_infotable(result):
                    print("    [XML via JSON index: {}]".format(name))
                    return result
                xml = _extract_xml_from_sgml(result)
                if xml:
                    print("    [XML via SGML en JSON index: {}]".format(name))
                    return xml
    except Exception:
        pass

    try:
        htm_url = "{}/Archives/edgar/data/{}/{}/{}-index.htm".format(
            ARCHIVES_BASE, cik_int, accession, acc_dashed)
        html = _get(htm_url, as_json=False, raise_on_error=False)
        if html:
            links = re.findall(r'href="(/Archives/[^"]+)"', html, re.IGNORECASE)
            for path in links:
                if any(k in path.lower() for k in ("infotable", "info_table", "informationtable")):
                    result = _get("{}{}".format(ARCHIVES_BASE, path), as_json=False, raise_on_error=False)
                    if result:
                        if _has_infotable(result):
                            print("    [XML via HTML index (infotable)]")
                            return result
                        xml = _extract_xml_from_sgml(result)
                        if xml:
                            print("    [SGML via HTML index (infotable)]")
                            return xml
            for path in links:
                if path.endswith('.xml') and 'primary' not in path.lower():
                    result = _get("{}{}".format(ARCHIVES_BASE, path), as_json=False, raise_on_error=False)
                    if result and _has_infotable(result):
                        print("    [XML via HTML index (fallback xml)]")
                        return result
            for path in links:
                if path.endswith('.txt') and 'primary' not in path.lower() and 'complete' not in path.lower():
                    result = _get("{}{}".format(ARCHIVES_BASE, path), as_json=False, raise_on_error=False)
                    if result:
                        xml = _extract_xml_from_sgml(result)
                        if xml:
                            print("    [SGML .txt via HTML index]")
                            return xml
    except Exception:
        pass

    raise ValueError("infotable no encontrado: CIK {} / {}".format(cik, accession))


def _find_file_in_index(idx, extensions=(".xml",)):
    items = idx.get("directory", {}).get("item", [])
    for item in items:
        n = item.get("name", "").lower()
        if "infotable" in n and any(n.endswith(ext) for ext in extensions):
            return item["name"]
    for item in items:
        n = item.get("name", "").lower()
        if any(n.endswith(ext) for ext in extensions) and "primary" not in n:
            return item["name"]
    return None


# ---- Parsing ----

def parse_infotable(xml_text):
    # FIX 1: Strip xmlns declarations
    xml_text = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_text)
    # FIX 2: Strip namespace-prefixed attributes (e.g. xsi:schemaLocation)
    # MUST come before tag stripping to avoid "unbound prefix" error in ET
    xml_text = re.sub(r'\s+\w+:\w+="[^"]*"', '', xml_text)
    # FIX 3: Strip namespace prefixes from opening tags: <ns1:infoTable> -> <infoTable>
    xml_text = re.sub(r'<(\w+):(\w+)', r'<\2', xml_text)
    # FIX 4: Strip namespace prefixes from closing tags: </ns1:infoTable> -> </infoTable>
    xml_text = re.sub(r'</(\w+):(\w+)', r'</\2', xml_text)

    root = ET.fromstring(xml_text)
    holdings = []
    rows = list(root.iter("infoTable"))
    if not rows:
        rows = list(root.iter("informationTable"))

    for row in rows:
        def get_text(tag, _row=row):
            el = _row.find(tag)
            if el is not None and el.text:
                return el.text.strip()
            return ""

        try:    value  = int(get_text("value") or 0) * 1000
        except: value  = 0
        try:    shares = int(get_text("sshPrnamt") or 0)
        except: shares = 0

        holdings.append({
            "company":  get_text("nameOfIssuer"),
            "cusip":    get_text("cusip"),
            "value":    value,
            "shares":   shares,
            "put_call": get_text("putCall"),
        })

    return holdings


def _fix_aum_scale(holdings):
    """
    Algunos filers (Bridgewater, Pershing) reportan en USD, no en miles.
    Detectamos si: avg > 50B, total > 5T, o precio/accion implicito > 100K.
    """
    if not holdings:
        return holdings
    total = sum(h["value"] for h in holdings)
    avg   = total / len(holdings)
    needs_fix = avg > 50_000_000_000 or total > 5_000_000_000_000
    if not needs_fix:
        valid = [h for h in holdings if h.get("shares", 0) > 0]
        if valid:
            sample = valid[:min(20, len(valid))]
            max_price = max(h["value"] / h["shares"] for h in sample)
            needs_fix = max_price > 100_000
    if needs_fix:
        print("    [AUM ajustado: valores en USD no miles -- dividiendo por 1000]")
        for h in holdings:
            h["value"] = h["value"] // 1000
    return holdings


def fetch_fund(fund_id, info, output_dir):
    cik  = info["cik"]
    name = info["name"]
    print("  -> {} (CIK {})".format(name, cik))

    filings = find_13f_filings(cik, max_results=2)
    if not filings:
        print("    Sin 13F-HR para {} -- CIK puede ser incorrecto".format(name))
        return None

    latest = filings[0]
    prev   = filings[1] if len(filings) > 1 else None
    print("    Filing: {} ({})".format(latest["accession"], latest["date"]))

    xml_curr = get_infotable_xml(cik, latest["accession"])
    curr_raw = parse_infotable(xml_curr)
    curr_raw = _fix_aum_scale(curr_raw)

    # prev_holdings: {cusip: {"shares": x, "company": y, "value": z}}
    prev_holdings = {}
    if prev is not None:
        try:
            xml_prev = get_infotable_xml(cik, prev["accession"])
            prev_raw = parse_infotable(xml_prev)
            prev_raw = _fix_aum_scale(prev_raw)
            for h in prev_raw:
                prev_holdings[h["cusip"]] = {
                    "shares":  h["shares"],
                    "company": h["company"],
                    "value":   h["value"],
                }
            print("    Trimestre anterior: {} posiciones".format(len(prev_holdings)))
        except Exception as e:
            print("    Trimestre anterior no disponible: {}".format(e))

    curr_cusips = {h["cusip"] for h in curr_raw}

    # CUSIPs vendidos: estaban en prev pero no en curr
    sold_cusips = [c for c in prev_holdings if c not in curr_cusips]

    all_cusips  = [h["cusip"] for h in curr_raw] + sold_cusips
    ticker_map  = cusip_to_tickers_batch(all_cusips)
    total_value = sum(h["value"] for h in curr_raw)
    holdings    = []

    # Posiciones actuales
    for h in curr_raw:
        cusip  = h["cusip"]
        ticker = ticker_map.get(cusip) or h["company"][:8].upper()
        pct    = (h["value"] / total_value * 100) if total_value > 0 else 0.0
        ph     = prev_holdings.get(cusip)
        ps     = ph["shares"] if ph else None

        if ps is None:
            activity = "new"
        elif h["shares"] > ps * 1.01:
            activity = "added"
        elif h["shares"] < ps * 0.99:
            activity = "reduced"
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
            "delta_shares":  (h["shares"] - ps) if ps is not None else None,
            "activity":      activity,
            "put_call":      h["put_call"],
        })

    # Posiciones vendidas: en prev, ausentes en curr
    for cusip in sold_cusips:
        ph     = prev_holdings[cusip]
        ticker = ticker_map.get(cusip) or ph["company"][:8].upper()
        holdings.append({
            "ticker":        ticker,
            "cusip":         cusip,
            "company":       ph["company"],
            "value":         ph["value"],   # valor del trimestre anterior
            "shares":        0,
            "portfolio_pct": 0.0,
            "prev_shares":   ph["shares"],
            "delta_shares":  -ph["shares"],
            "activity":      "sold",
            "put_call":      "",
        })

    if sold_cusips:
        print("    Posiciones vendidas detectadas: {}".format(len(sold_cusips)))

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
    with open(os.path.join(output_dir, "{}.json".format(fund_id)), "w") as f:
        json.dump(result, f, indent=2)

    print("    {} holdings | AUM ~${:.1f}B | {}".format(
        len(holdings), total_value / 1e9, latest["date"]))
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
