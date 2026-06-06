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
    "appaloosa":    {"name": "Appaloosa Management",    "cik": "0001070154"},
    "gotham":       {"name": "Gotham Asset Management", "cik": "0001579982"},
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
            time.sleep(0.6)
            r = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": c} for c in batch],
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
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
    """Extrae 13F-HR de un objeto de filings recientes."""
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

    # Loguear nombre para verificar CIK correcto
    api_name = data.get("name", "unknown")
    print("    API name: {}".format(api_name))

    recent = data.get("filings", {}).get("recent", {})
    filings = _extract_filings_from_page(recent)

    # Si no hay 13F en los ultimos ~40 filings, buscar en paginas anteriores
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

def _extract_xml_from_sgml(text):
    """
    Extrae infotable XML embebido en un archivo .txt SGML.
    Formato EDGAR: <XML>...contenido XML...</XML>
    """
    # Buscar bloque XML dentro del SGML
    m = re.search(r'<XML>(.*?)</XML>', text, re.DOTALL | re.IGNORECASE)
    if m:
        xml_content = m.group(1).strip()
        if '<infoTable>' in xml_content or '<informationTable>' in xml_content:
            return xml_content
    # Alternativa: XML directo sin wrapper
    if '<infoTable>' in text:
        start = text.find('<?xml')
        if start < 0:
            start = text.find('<informationTable')
        if start < 0:
            start = text.find('<infoTable')
        if start >= 0:
            return text[start:]
    return None


def get_infotable_xml(cik, accession):
    cik_int    = str(int(cik))
    acc_dashed = "{}-{}-{}".format(accession[:10], accession[10:12], accession[12:])
    base_path  = "{}/Archives/edgar/data/{}/{}".format(ARCHIVES_BASE, cik_int, accession)

    # Estrategia 1: nombres de archivo comunes (.xml)
    for fname in ("infotable.xml", "form13fInfoTable.xml", "13F_InfoTable.xml",
                  "13f_InfoTable.xml", "informationtable.xml"):
        result = _get("{}/{}".format(base_path, fname), as_json=False, raise_on_error=False)
        if result and ('<infoTable>' in result or '<informationTable>' in result):
            print("    [XML: {}]".format(fname))
            return result

    # Estrategia 2: JSON index
    try:
        idx_url = "https://data.sec.gov/Archives/edgar/data/{}/{}/{}-index.json".format(
            cik_int, accession, acc_dashed)
        idx = _get(idx_url)
        name = _find_file_in_index(idx, extensions=(".xml", ".txt", ".htm"))
        if name:
            result = _get("{}/{}".format(base_path, name), as_json=False)
            if result:
                if '<infoTable>' in result or '<informationTable>' in result:
                    print("    [XML via JSON index: {}]".format(name))
                    return result
                # Puede ser SGML .txt
                xml = _extract_xml_from_sgml(result)
                if xml:
                    print("    [XML via SGML en JSON index: {}]".format(name))
                    return xml
    except Exception:
        pass

    # Estrategia 3: HTML index — buscar .xml, .txt y .htm
    try:
        htm_url = "{}/Archives/edgar/data/{}/{}/{}-index.htm".format(
            ARCHIVES_BASE, cik_int, accession, acc_dashed)
        html = _get(htm_url, as_json=False, raise_on_error=False)
        if html:
            # Buscar por nombre de infotable (prioridad)
            links = re.findall(r'href="(/Archives/[^"]+)"', html, re.IGNORECASE)
            for path in links:
                if any(k in path.lower() for k in ("infotable", "info_table", "informationtable")):
                    result = _get("{}{}".format(ARCHIVES_BASE, path), as_json=False, raise_on_error=False)
                    if result:
                        if '<infoTable>' in result or '<informationTable>' in result:
                            print("    [XML via HTML index (infotable)]")
                            return result
                        xml = _extract_xml_from_sgml(result)
                        if xml:
                            print("    [SGML via HTML index (infotable)]")
                            return xml

            # Fallback: cualquier .xml que no sea primary
            for path in links:
                if path.endswith('.xml') and 'primary' not in path.lower():
                    result = _get("{}{}".format(ARCHIVES_BASE, path), as_json=False, raise_on_error=False)
                    if result and ('<infoTable>' in result or '<informationTable>' in result):
                        print("    [XML via HTML index (fallback xml)]")
                        return result

            # Fallback .txt: formato SGML antiguo
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
    # Prioridad: infotable
    for item in items:
        n = item.get("name", "").lower()
        if "infotable" in n and any(n.endswith(ext) for ext in extensions):
            return item["name"]
    # Fallback: cualquier archivo con extension válida que no sea primary_doc
    for item in items:
        n = item.get("name", "").lower()
        if any(n.endswith(ext) for ext in extensions) and "primary" not in n:
            return item["name"]
    return None


# ---- Parsing ----

def parse_infotable(xml_text):
    # Normalizar tag raiz (puede ser informationTable o infoTable)
    xml_text = xml_text.replace('xmlns="', 'xmlns_removed="')
    root = ET.fromstring(xml_text)

    holdings = []
    # Soportar ambos: infoTable (moderno) e informationTable (antiguo)
    rows = list(root.iter("infoTable")) or list(root.iter("infoTable".lower()))
    if not rows:
        rows = list(root.iter("informationTable"))

    for row in root.iter("infoTable"):
        def get_text(tag):
            el = row.find(tag)
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
    if not holdings:
        return holdings
    avg = sum(h["value"] for h in holdings) / len(holdings)
    if avg > 50_000_000_000:
        print("    [AUM ajustado: escala parece USD, no miles]")
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

    prev_shares = {}
    if prev is not None:
        try:
            xml_prev = get_infotable_xml(cik, prev["accession"])
            for h in parse_infotable(xml_prev):
                prev_shares[h["cusip"]] = h["shares"]
        except Exception as e:
            print("    Trimestre anterior no disponible: {}".format(e))

    all_cusips  = [h["cusip"] for h in curr_raw]
    ticker_map  = cusip_to_tickers_batch(all_cusips)
    total_value = sum(h["value"] for h in curr_raw)
    holdings    = []

    for h in curr_raw:
        cusip  = h["cusip"]
        ticker = ticker_map.get(cusip) or h["company"][:8].upper()
        pct    = (h["value"] / total_value * 100) if total_value > 0 else 0.0
        ps     = prev_shares.get(cusip)

        if ps is None:          activity = "new"
        elif h["shares"] > ps * 1.01: activity = "added"
        elif h["shares"] < ps * 0.99: activity = "reduced"
        else:                   activity = "hold"

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
