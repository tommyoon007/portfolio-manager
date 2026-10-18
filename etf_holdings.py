#!/usr/bin/env python3
"""Fetch official SMH/SOXX holdings and weights into etf_holdings.json."""
import csv, io, json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests

OUT = Path('etf_holdings.json')
EMAIL = (os.environ.get('SEC_CONTACT_EMAIL') or '').strip()
UA = f'PortfolioManager/1.0 (contact: {EMAIL})' if EMAIL else 'PortfolioManager/1.0 (ETF holdings data collector)'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/json,text/csv,*/*'}
URLS = {
  'SMH': 'https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/',
  'SOXX': 'https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf',
}

def clean_ticker(v):
    s = str(v or '').strip().upper()
    if not s or s in {'-USD CASH-', 'CASH', 'OTHER', 'USD CASH', '--'}: return None
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', s): return None
    return s

def parse_weight(v):
    s = str(v or '').replace(',', '').replace('%','').strip()
    try:
        n = float(s)
        return n if 0 < n <= 100 else None
    except Exception: return None

def fetch_html_tables(ticker, url):
    """Parse provider tables directly from HTML; tolerate multi-row and nested headers."""
    from bs4 import BeautifulSoup
    r = requests.get(url, headers=HEADERS, timeout=35)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    candidates = []

    # First parse actual HTML tables. This avoids pandas' changing header inference.
    for table in soup.find_all('table'):
        trs = table.find_all('tr')
        if not trs: continue
        grid = []
        for tr in trs:
            cells = tr.find_all(['th', 'td'])
            vals = [' '.join(c.stripped_strings).strip() for c in cells]
            if vals: grid.append(vals)
        if not grid: continue
        header_idx = None
        ti = wi = None
        for ri, vals in enumerate(grid[:8]):
            joined = ' | '.join(vals).lower()
            if ('ticker' in joined or 'symbol' in joined) and ('weight' in joined or 'net assets' in joined or 'portfolio weight' in joined):
                header_idx = ri
                ti = next((i for i,v in enumerate(vals) if 'ticker' in v.lower() or 'symbol' in v.lower()), None)
                wi = next((i for i,v in enumerate(vals) if 'weight' in v.lower() or 'net assets' in v.lower() or 'portfolio weight' in v.lower()), None)
                break
        if header_idx is None:
            # Some provider tables use separate/multi-level headers; combine up to 3 rows.
            for ri in range(min(3, len(grid))):
                vals = []
                for ci in range(max(len(x) for x in grid[ri:ri+3])):
                    vals.append(' '.join(row[ci] for row in grid[ri:ri+3] if ci < len(row)))
                joined = ' | '.join(vals).lower()
                if ('ticker' in joined or 'symbol' in joined) and ('weight' in joined or 'net assets' in joined):
                    header_idx = ri
                    ti = next((i for i,v in enumerate(vals) if 'ticker' in v.lower() or 'symbol' in v.lower()), None)
                    wi = next((i for i,v in enumerate(vals) if 'weight' in v.lower() or 'net assets' in v.lower()), None)
                    break
        if header_idx is None or ti is None or wi is None: continue
        rows=[]
        for vals in grid[header_idx+1:]:
            if ti >= len(vals) or wi >= len(vals): continue
            sym=clean_ticker(vals[ti]); weight=parse_weight(vals[wi])
            if sym and weight is not None: rows.append({'ticker':sym,'weight_pct':weight})
        if len(rows) >= 10: candidates.append(rows)

    # Fallback: provider pages sometimes expose holdings as pipe-separated table text.
    if not candidates:
        text = soup.get_text('\n', strip=True)
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        in_holdings = False
        rows=[]
        for line in lines:
            low=line.lower()
            if 'ticker' in low and ('% of net assets' in low or 'weight (%)' in low):
                in_holdings=True; continue
            if in_holdings:
                parts = [x.strip() for x in re.split(r'\s*\|\s*', line)]
                if len(parts) >= 3:
                    sym=clean_ticker(parts[0]); weight=parse_weight(parts[2] if ticker=='SMH' else parts[4] if len(parts)>4 else parts[-1])
                    if sym and weight is not None: rows.append({'ticker':sym,'weight_pct':weight})
                if len(rows) >= 10 and ('scroll' in low or 'fees' in low): break
        if len(rows) >= 10: candidates.append(rows)

    if not candidates:
        raise RuntimeError(f'{ticker}: holdings table not found in returned HTML (HTTP {r.status_code}, {len(r.text)} chars). Provider may be serving a JS-only or bot-blocked page.')
    return max(candidates, key=len)

def main():
    try:
        import pandas  # noqa
    except ImportError:
        raise SystemExit('Missing pandas. Install with: pip install requests pandas lxml html5lib')
    result={}
    errors={}
    for ticker,url in URLS.items():
        try:
            rows=fetch_html_tables(ticker,url)
            # Deduplicate rows and normalize to the sum of listed equity weights.
            ded={}
            for row in rows: ded[row['ticker']]=row['weight_pct']
            total=sum(ded.values())
            if len(ded)<10 or not (70 <= total <= 110):
                raise RuntimeError(f'validation failed: {len(ded)} tickers, total weight={total:.2f}%')
            result[ticker]={
                'source_url':url,
                'holdings_date':datetime.now(timezone.utc).date().isoformat(),
                'retrieved_at':datetime.now(timezone.utc).isoformat(),
                'holdings':[{'ticker':k,'weight_pct':round(v,6)} for k,v in sorted(ded.items(),key=lambda x:x[1],reverse=True)],
                'count':len(ded),
                'total_weight_pct':round(total,4),
            }
            print(f'OK {ticker}: {len(ded)} holdings, total weight {total:.2f}%')
        except Exception as exc:
            errors[ticker]=str(exc)
            print(f'FAIL {ticker}: {exc}')
    # Keep last known good data if a provider temporarily changes its page.
    if OUT.exists():
        try:
            old=json.loads(OUT.read_text(encoding='utf-8'))
            for ticker,record in old.get('data',{}).items():
                if ticker not in result: result[ticker]=record
        except Exception: pass
    if not result: raise RuntimeError('No valid ETF holdings obtained; refusing to overwrite with empty data.')
    payload={'updated_at':datetime.now(timezone.utc).isoformat(),'source':'Official issuer holdings pages','data':result,'errors':errors}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(f'Wrote {OUT}; datasets={list(result)}; errors={list(errors)}')
    if errors: print('WARNING: at least one ETF dataset used cached data or is unavailable.')

if __name__=='__main__': main()
