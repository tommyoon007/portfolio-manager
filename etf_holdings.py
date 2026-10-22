#!/usr/bin/env python3
"""Download official SMH/SOXX holdings files and write etf_holdings.json.

SMH: follows the official VanEck page's holdings download link (XLS/XLSX/CSV).
SOXX: downloads the official iShares holdings CSV endpoint.
Keeps a prior valid dataset for any provider that temporarily fails.
"""
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUT = Path("etf_holdings.json")
CONTACT = (os.environ.get("SEC_CONTACT_EMAIL") or "").strip()
HEADERS = {
    "User-Agent": f"PortfolioManager ETF data collector ({CONTACT})" if CONTACT else "Mozilla/5.0 (compatible; PortfolioManagerETF/1.0)",
    "Accept": "text/html,application/json,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
}
VANECK_PAGE = "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/"
ISHARES_CSV = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax?fileType=csv&fileName=SOXX_holdings&dataType=fund"


def norm(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip().lower()


def ticker_clean(value):
    s = str(value or "").strip().upper()
    if not s or s in {"-USD CASH-", "USD CASH", "CASH", "OTHER", "--", "N/A", "NAN"}:
        return None
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
        return None
    return s


def number(value):
    s = str(value or "").strip().replace(",", "").replace("%", "").replace("$", "")
    if not s or s in {"-", "—", "N/A", "nan", "None"}:
        return None
    try:
        n = float(s)
        return n if 0 < n <= 100 else None
    except (TypeError, ValueError):
        return None


def find_columns(headers):
    headers = [norm(x) for x in headers]
    ti = next((i for i, h in enumerate(headers) if h in {"ticker", "symbol", "ticker symbol"} or h.endswith(" ticker")), None)
    wi = next((i for i, h in enumerate(headers) if "weight" in h or "net assets" in h or "portfolio percentage" in h or h == "% of net assets"), None)
    return ti, wi


def records_from_dataframe(df):
    # Search first 15 rows for a header, because issuer spreadsheets often include title/date rows.
    rows = df.fillna("").astype(str).values.tolist()
    for hi, row in enumerate(rows[:15]):
        ti, wi = find_columns(row)
        if ti is None or wi is None:
            continue
        out = []
        for data in rows[hi + 1:]:
            if ti >= len(data) or wi >= len(data):
                continue
            t = ticker_clean(data[ti])
            w = number(data[wi])
            if t and w is not None:
                out.append({"ticker": t, "weight_pct": w})
        if len(out) >= 10:
            return out
    return []


def parse_csv_bytes(content):
    # iShares CSV begins with fund metadata, then the actual holdings header.
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    for start, line in enumerate(lines[:80]):
        try:
            cells = next(csv.reader([line]))
        except Exception:
            continue
        ti, wi = find_columns(cells)
        if ti is not None and wi is not None:
            try:
                df = pd.read_csv(io.StringIO("\n".join(lines[start:])), header=0)
                rows = records_from_dataframe(df)
                if len(rows) >= 10:
                    return rows
            except Exception:
                pass
            out = []
            for line2 in lines[start + 1:]:
                try:
                    cells2 = next(csv.reader([line2]))
                except Exception:
                    continue
                if ti < len(cells2) and wi < len(cells2):
                    t, w = ticker_clean(cells2[ti]), number(cells2[wi])
                    if t and w is not None:
                        out.append({"ticker": t, "weight_pct": w})
            if len(out) >= 10:
                return out
    return []


def parse_json_bytes(content):
    """Parse JSON holdings responses returned by provider download endpoints."""
    try:
        obj = json.loads(content.decode("utf-8-sig", errors="replace"))
    except Exception:
        return []

    ticker_keys = {"ticker", "symbol", "tickersymbol", "securityticker"}
    weight_keys = {
        "weight", "weightpct", "weightpercent", "portfolioweight",
        "portfolio_percentage", "percentofnetassets", "netassetspercent",
        "percentageofnetassets", "holdingweight",
    }

    def key_norm(key):
        return re.sub(r"[^a-z0-9]", "", str(key).lower())

    def walk(value):
        if isinstance(value, dict):
            normalized = {key_norm(k): v for k, v in value.items()}
            ticker = next((normalized[k] for k in ticker_keys if k in normalized), None)
            weight = next((normalized[k] for k in weight_keys if k in normalized), None)
            if ticker is not None and weight is not None:
                t = ticker_clean(ticker)
                w = number(weight)
                if t and w is not None:
                    yield {"ticker": t, "weight_pct": w}
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    rows = list(walk(obj))
    # Deduplicate nested representations while retaining the last reported weight.
    dedup = {}
    for row in rows:
        dedup[row["ticker"]] = row["weight_pct"]
    return [{"ticker": t, "weight_pct": w} for t, w in dedup.items()]


def parse_download(content, url, content_type=""):
    low = (url + " " + content_type).lower()
    if "json" in low or content.lstrip().startswith((b"{", b"[")):
        rows = parse_json_bytes(content)
        if len(rows) >= 10:
            return rows
    if "csv" in low or content[:200].decode("utf-8", errors="ignore").lower().find("ticker") >= 0:
        rows = parse_csv_bytes(content)
        if len(rows) >= 10:
            return rows
    # XLS/XLSX; try all sheets and rows because issuer files may have title lines.
    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
        for df in sheets.values():
            rows = records_from_dataframe(df)
            if len(rows) >= 10:
                return rows
    except Exception:
        pass
    # HTML download endpoints can sometimes return a rendered table.
    try:
        tables = pd.read_html(io.BytesIO(content), header=None)
        for df in tables:
            rows = records_from_dataframe(df)
            if len(rows) >= 10:
                return rows
    except Exception:
        pass
    return []


def parse_html_holdings(html):
    """Extract ticker/weight rows directly from official HTML tables."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_index = None
        ticker_index = weight_index = None
        for i, tr in enumerate(rows[:25]):
            cells = tr.find_all(["th", "td"])
            labels = [norm(cell.get_text(" ", strip=True)) for cell in cells]
            ti, wi = find_columns(labels)
            if ti is not None and wi is not None:
                header_index, ticker_index, weight_index = i, ti, wi
                break
        if header_index is None:
            continue
        table_rows = []
        for tr in rows[header_index + 1:]:
            cells = tr.find_all(["th", "td"])
            if max(ticker_index, weight_index) >= len(cells):
                continue
            ticker = ticker_clean(cells[ticker_index].get_text(" ", strip=True))
            weight = number(cells[weight_index].get_text(" ", strip=True))
            if ticker and weight is not None:
                table_rows.append({"ticker": ticker, "weight_pct": weight})
        if len(table_rows) >= 10:
            found.extend(table_rows)
    if not found:
        return []
    dedup = {}
    for row in found:
        dedup[row["ticker"]] = row["weight_pct"]
    return [{"ticker": t, "weight_pct": w} for t, w in dedup.items()]


def parse_embedded_holdings(html):
    """Find holdings in JSON embedded in the official page, including script state."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text:
            continue
        text = text.strip()
        # Try complete JSON script blocks and common HTML-embedded state objects.
        for candidate in (text,):
            try:
                obj = json.loads(candidate)
            except Exception:
                obj = None
            if obj is not None:
                rows = parse_json_object_holdings(obj)
                if len(rows) >= 10:
                    candidates.append(rows)
        # Extract object-like JSON snippets when the script wraps state in JS.
        for key in ("holdings", "Holdings", "holdingsData", "HoldingsData", "portfolioHoldings"):
            for match in re.finditer(r'["\']?' + re.escape(key) + r'["\']?\s*:\s*(\[.*?\])', text, flags=re.I | re.S):
                raw = match.group(1)
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                rows = parse_json_object_holdings(obj)
                if len(rows) >= 10:
                    candidates.append(rows)
    if not candidates:
        return []
    return max(candidates, key=len)


def parse_json_object_holdings(obj):
    ticker_keys = {"ticker", "symbol", "tickersymbol", "securityticker", "securitysymbol"}
    weight_keys = {"weight", "weightpct", "weightpercent", "portfolioweight", "portfolio_percentage",
                   "percentofnetassets", "netassetspercent", "percentageofnetassets", "holdingweight", "weighting"}
    def kn(k):
        return re.sub(r"[^a-z0-9]", "", str(k).lower())
    rows = []
    def walk(v):
        if isinstance(v, dict):
            d = {kn(k): val for k, val in v.items()}
            t = next((d[k] for k in ticker_keys if k in d), None)
            w = next((d[k] for k in weight_keys if k in d), None)
            if t is not None and w is not None:
                ticker, weight = ticker_clean(t), number(w)
                if ticker and weight is not None:
                    rows.append({"ticker": ticker, "weight_pct": weight})
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(obj)
    dedup = {}
    for row in rows:
        dedup[row["ticker"]] = row["weight_pct"]
    return [{"ticker": t, "weight_pct": w} for t, w in dedup.items()]

def fetch_smh():
    """Fetch SMH holdings from VanEck's official XLSX endpoint first."""
    # VanEck's current site exposes the daily workbook at this endpoint.
    # The page URL is retained as a fallback in case the endpoint changes.
    direct_urls = [
        "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/downloads/holdings/",
        "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/holdings/",
        VANECK_PAGE,
    ]
    last_error = None
    for url in direct_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=45, allow_redirects=True)
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "")
            # First try to parse as the XLSX/CSV/JSON workbook response.
            rows = parse_download(r.content, r.url, content_type)
            if len(rows) >= 10:
                print(f"OK SMH source: {r.url} ({len(rows)} parsed rows)")
                return rows, r.url

            # If the endpoint redirected to an HTML page, parse the page itself.
            if "html" in content_type.lower() or r.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                rows = parse_html_holdings(r.text)
                if len(rows) >= 10:
                    return rows, r.url
                rows = parse_embedded_holdings(r.text)
                if len(rows) >= 10:
                    return rows, r.url
                soup = BeautifulSoup(r.text, "html.parser")
                # Follow download links found on each official page.
                links = []
                for tag in soup.find_all(["a", "button"]):
                    href = tag.get("href") or tag.get("data-href") or tag.get("data-url") or ""
                    label = norm(" ".join(tag.stripped_strings))
                    combined = (label + " " + href).lower()
                    if any(word in combined for word in ("holding", "download", "export")):
                        if any(ext in combined for ext in (".xls", ".xlsx", ".csv", "download", "export")):
                            links.append(requests.compat.urljoin(r.url, href))
                # Some VanEck pages expose workbook URLs in serialized page data.
                for match in re.findall(r"([^\"'<> ]*(?:holding|download|export)[^\"'<> ]*\.(?:xlsx?|csv)(?:\?[^\"'<> ]*)?)", r.text, flags=re.I):
                    links.append(requests.compat.urljoin(r.url, match.replace("\\\\u0026", "&")))
                for link in dict.fromkeys(links):
                    try:
                        dl = requests.get(link, headers=HEADERS, timeout=45, allow_redirects=True)
                        dl.raise_for_status()
                        rows = parse_download(dl.content, dl.url, dl.headers.get("Content-Type", ""))
                        if len(rows) >= 10:
                            return rows, dl.url
                    except Exception as exc:
                        print(f"SMH download candidate failed: {link} ({exc})")
                # pandas HTML table fallback.
                try:
                    tables = pd.read_html(io.StringIO(r.text), header=None)
                except Exception:
                    tables = []
                for df in tables:
                    rows = records_from_dataframe(df)
                    if len(rows) >= 10:
                        return rows, r.url
            else:
                print(f"SMH endpoint returned unparsed content: {r.url}; type={content_type}; bytes={len(r.content)}")
        except Exception as exc:
            last_error = exc
            print(f"SMH source candidate failed: {url} ({exc})")

    if last_error:
        print(f"SMH last request error: {last_error}")
    raise RuntimeError("SMH: could not parse official VanEck holdings download/page after trying official XLSX endpoint and page fallbacks")


def fetch_soxx():
    response = requests.get(ISHARES_CSV, headers=HEADERS, timeout=45)
    response.raise_for_status()
    rows = parse_download(response.content, response.url, response.headers.get("Content-Type", ""))
    if len(rows) < 10:
        # Fallback: find official CSV download link on the official iShares page.
        page = requests.get("https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf", headers=HEADERS, timeout=45)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        links = []
        for tag in soup.find_all(["a", "button"]):
            href = tag.get("href") or tag.get("data-href") or tag.get("data-url") or ""
            combined = (norm(" ".join(tag.stripped_strings)) + " " + href).lower()
            if "csv" in combined and ("holding" in combined or "filetype=csv" in combined):
                links.append(requests.compat.urljoin(page.url, href))
        for url in links:
            try:
                r = requests.get(url, headers=HEADERS, timeout=45)
                r.raise_for_status()
                rows = parse_download(r.content, r.url, r.headers.get("Content-Type", ""))
                if len(rows) >= 10:
                    return rows, r.url
            except Exception as exc:
                print(f"SOXX CSV candidate failed: {url} ({exc})")
        raise RuntimeError(f"SOXX: official iShares CSV did not parse (HTTP {response.status_code}, {len(response.content)} bytes)")
    return rows, response.url


def validate_rows(ticker, rows):
    dedup = {}
    for row in rows:
        dedup[row["ticker"]] = row["weight_pct"]
    total = sum(dedup.values())
    if len(dedup) < 10 or not (70 <= total <= 110):
        raise RuntimeError(f"{ticker}: validation failed ({len(dedup)} tickers, total weight {total:.2f}%)")
    return [{"ticker": t, "weight_pct": round(w, 6)} for t, w in sorted(dedup.items(), key=lambda x: x[1], reverse=True)], total


def main():
    result = {}
    errors = {}
    fetchers = {"SMH": fetch_smh, "SOXX": fetch_soxx}
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8")).get("data", {})
        except Exception:
            old = {}
    for ticker, fetcher in fetchers.items():
        try:
            rows, source_url = fetcher()
            clean, total = validate_rows(ticker, rows)
            result[ticker] = {
                "source_url": source_url,
                "holdings_date": None,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "holdings": clean,
                "count": len(clean),
                "total_weight_pct": round(total, 4),
            }
            print(f"OK {ticker}: {len(clean)} holdings, total weight {total:.2f}%")
        except Exception as exc:
            errors[ticker] = str(exc)
            print(f"FAIL {ticker}: {exc}")
            if ticker in old:
                result[ticker] = old[ticker]
                print(f"Using last known good {ticker} dataset.")
    if not result:
        raise RuntimeError("No valid ETF holdings obtained; refusing to overwrite with empty data.")
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Official VanEck/iShares holdings downloads",
        "data": result,
        "errors": errors,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}; datasets={list(result)}; errors={list(errors)}")
    if errors:
        print("WARNING: one or more ETF datasets used cached data or are unavailable.")


if __name__ == "__main__":
    main()
