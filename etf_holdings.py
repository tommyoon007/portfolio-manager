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
    r = requests.get(url, headers=HEADERS, timeout=35)
    r.raise_for_status()
    html = r.text
    # pandas can parse provider tables without hard-coding changing row positions.
    import pandas as pd
    tables = pd.read_html(io.StringIO(html))
    candidates = []
    for table in tables:
        # Provider tables may use multi-row headers; flatten every header level.
        def flatten(col):
            parts = col if isinstance(col, tuple) else (col,)
            return ' '.join(str(p).strip() for p in parts
                            if str(p).strip() and not str(p).lower().startswith('unnamed')).lower()
        cols = [flatten(c) for c in table.columns]
        ticker_idx = next((i for i,c in enumerate(cols) if 'ticker' in c or 'symbol' in c), None)
        weight_idx = next((i for i,c in enumerate(cols) if 'weight' in c or 'net assets' in c or 'portfolio weight' in c), None)
        if ticker_idx is None or weight_idx is None: continue
        rows=[]
        for _, row in table.iterrows():
            sym=clean_ticker(row.iloc[ticker_idx])
            weight=parse_weight(row.iloc[weight_idx])
            if sym and weight is not None: rows.append({'ticker':sym,'weight_pct':weight})
        if len(rows) >= 10:
            candidates.append(rows)
    if not candidates: raise RuntimeError(f'{ticker}: could not identify a holdings table from official provider page')
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
