import json
import os
import time
import math
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path('sec_financials.json')
TICKERS_FILE = Path('sec_tickers.json')

# SEC requires a declared User-Agent with a meaningful contact.
UA = os.environ.get(
    'SEC_USER_AGENT',
    'PortfolioManager/1.0 (GitHub Actions; https://github.com/tommyoon007/portfolio-manager)'
)
HEADERS = {
    'User-Agent': UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Encoding': 'gzip, deflate',
    'Host': 'www.sec.gov',
}
DATA_HEADERS = dict(HEADERS)
DATA_HEADERS['Host'] = 'data.sec.gov'

COMMON = 'https://data.sec.gov'


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def get_json(url, headers, timeout=(10, 20)):
    """Fetch one SEC JSON document with a hard timeout and no retry loop.

    A single failed ticker must never hold the whole GitHub Action open for
    minutes. The caller records the failure and continues with other tickers.
    """
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"SEC request failed: {url} — {exc}") from exc

    content_type = (r.headers.get('content-type') or '').lower()

    if r.status_code != 200:
        body = r.text[:160].replace('\n', ' ')
        raise RuntimeError(
            f"SEC HTTP {r.status_code} for {url}; "
            f"content-type={content_type}; body={body}"
        )

    text = r.text.lstrip('\ufeff').strip()
    if not text:
        raise RuntimeError(f"Empty SEC response: {url}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:160].replace('\n', ' ')
        raise RuntimeError(
            f"SEC returned non-JSON content for {url}; "
            f"content-type={content_type}; preview={preview}"
        ) from exc


def duration_facts(arr):
    out = []
    for x in arr or []:
        if x.get('start') and x.get('end') and num(x.get('val')) is not None:
            y = dict(x)
            y['val'] = num(x['val'])
            out.append(y)
    return sorted(out, key=lambda x: (x.get('end', ''), x.get('filed', '')))


def instant_facts(arr):
    out = []
    for x in arr or []:
        if not x.get('start') and num(x.get('val')) is not None:
            y = dict(x)
            y['val'] = num(x['val'])
            out.append(y)
    return sorted(out, key=lambda x: (x.get('end', ''), x.get('filed', '')))


def quarter_values(arr):
    q = []
    for r in duration_facts(arr):
        try:
            start = datetime.fromisoformat(r['start'])
            end = datetime.fromisoformat(r['end'])
            days = (end - start).days
        except Exception:
            continue
        if 70 <= days <= 120:
            q.append(r)
    ded = {}
    for r in q:
        ded[r['end']] = r
    return sorted(ded.values(), key=lambda x: x['end'])


def ttm_from_quarters(arr):
    q = quarter_values(arr)
    if len(q) >= 4:
        return sum(x['val'] for x in q[-4:])
    return None


def latest_q_growth(arr):
    q = quarter_values(arr)
    if len(q) < 5:
        return None
    a, b = q[-1]['val'], q[-5]['val']
    if b == 0:
        return None
    return (a / b - 1) * 100


def fact_latest(arr):
    rows = instant_facts(arr)
    return rows[-1]['val'] if rows else None


def fact_duration_latest(arr):
    rows = duration_facts(arr)
    return rows[-1]['val'] if rows else None


def arr_for(facts_all, taxonomy, tags, unit):
    base = facts_all.get(taxonomy, {})
    for tag in tags:
        obj = base.get(tag, {})
        arr = obj.get('units', {}).get(unit, [])
        if arr:
            return arr
    return []


def build_company(ticker, cik, payload):
    facts_all = payload.get('facts', {})

    revenue_arr = arr_for(facts_all, 'us-gaap', [
        'RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'
    ], 'USD')
    ni_arr = arr_for(facts_all, 'us-gaap', [
        'NetIncomeLoss', 'ProfitLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic'
    ], 'USD')
    ocf_arr = arr_for(facts_all, 'us-gaap', [
        'NetCashProvidedByUsedInOperatingActivities'
    ], 'USD')
    capex_arr = arr_for(facts_all, 'us-gaap', [
        'PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets'
    ], 'USD')
    opinc_arr = arr_for(facts_all, 'us-gaap', ['OperatingIncomeLoss'], 'USD')
    interest_arr = arr_for(facts_all, 'us-gaap', [
        'InterestExpenseNonOperating', 'InterestExpenseNonOperatingAndOperating'
    ], 'USD')
    da_arr = arr_for(facts_all, 'us-gaap', [
        'DepreciationDepletionAndAmortization',
        'DepreciationDepletionAndAmortizationPropertyPlantAndEquipment'
    ], 'USD')

    revenue_ttm = ttm_from_quarters(revenue_arr) or fact_duration_latest(revenue_arr)
    ni_ttm = ttm_from_quarters(ni_arr) or fact_duration_latest(ni_arr)
    ocf_ttm = ttm_from_quarters(ocf_arr) or fact_duration_latest(ocf_arr)
    capex_ttm = ttm_from_quarters(capex_arr) or fact_duration_latest(capex_arr)
    fcf = (ocf_ttm - abs(capex_ttm)) if ocf_ttm is not None and capex_ttm is not None else None

    opinc_ttm = ttm_from_quarters(opinc_arr) or fact_duration_latest(opinc_arr)
    interest_ttm = ttm_from_quarters(interest_arr) or fact_duration_latest(interest_arr)
    da_ttm = ttm_from_quarters(da_arr) or fact_duration_latest(da_arr)
    ebitda = (opinc_ttm + abs(da_ttm)) if opinc_ttm is not None and da_ttm is not None else None

    us = facts_all.get('us-gaap', {})
    assets = fact_latest(us.get('Assets', {}).get('units', {}).get('USD', []))
    liabilities = fact_latest(us.get('Liabilities', {}).get('units', {}).get('USD', []))
    equity = None
    for tag in ['StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest']:
        equity = fact_latest(us.get(tag, {}).get('units', {}).get('USD', []))
        if equity is not None:
            break

    cash = None
    for tag in ['CashAndCashEquivalentsAtCarryingValue',
                'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents']:
        cash = fact_latest(us.get(tag, {}).get('units', {}).get('USD', []))
        if cash is not None:
            break

    ca = fact_latest(us.get('AssetsCurrent', {}).get('units', {}).get('USD', []))
    cl = fact_latest(us.get('LiabilitiesCurrent', {}).get('units', {}).get('USD', []))

    debt = 0
    for tag in ['LongTermDebtAndFinanceLeaseObligationsCurrent',
                'LongTermDebtCurrent',
                'LongTermDebtNoncurrent',
                'LongTermDebtAndFinanceLeaseObligationsNoncurrent']:
        v = fact_latest(us.get(tag, {}).get('units', {}).get('USD', []))
        if v is not None:
            debt += max(0, v)

    dei = facts_all.get('dei', {})
    shobj = dei.get('EntityCommonStockSharesOutstanding', {}).get('units', {}).get('shares', [])
    shares = instant_facts(shobj)[-1]['val'] if instant_facts(shobj) else None

    return {
        'RevenueTTM': revenue_ttm,
        'QuarterlyRevenueGrowthYOY': latest_q_growth(revenue_arr),
        'QuarterlyEarningsGrowthYOY': latest_q_growth(ni_arr),
        'OperatingIncomeTTM': opinc_ttm,
        'NetIncomeTTM': ni_ttm,
        'OperatingMarginTTM': (opinc_ttm / revenue_ttm * 100) if opinc_ttm is not None and revenue_ttm else None,
        'ReturnOnEquityTTM': (ni_ttm / equity * 100) if ni_ttm is not None and equity not in (None, 0) else None,
        'DebtToEquity': (debt / equity * 100) if equity not in (None, 0) else None,
        'CurrentRatio': (ca / cl) if ca is not None and cl not in (None, 0) else None,
        'OperatingCashflow': ocf_ttm,
        'FreeCashFlowTTM': fcf,
        'EBITDA': ebitda,
        'InterestExpenseTTM': abs(interest_ttm) if interest_ttm is not None else None,
        'SharesOutstanding': shares,
        'DilutedSharesOutstanding': shares,
        'Beta': None,
        'ForwardPE': None,
        'PERatio': None,
        'PEGRatio': None,
        'PriceToSalesRatioTTM': None,
        'EVToEBITDA': None,
        'MarketCapitalization': None,
        'TotalDebt': debt,
        'Cash': cash,
        '__source': 'SEC EDGAR XBRL companyfacts',
        '__cik': cik,
        '__updatedAt': datetime.now(timezone.utc).isoformat(),
    }


def main():
    tickers = json.loads(TICKERS_FILE.read_text(encoding='utf-8'))
    if not isinstance(tickers, list):
        raise RuntimeError('sec_tickers.json must contain a JSON array of ticker symbols.')

    # The workflow downloads a daily pre-generated ticker→CIK mapping.
    # The actual financial facts remain sourced directly from SEC data.sec.gov.
    mapping_file = Path('sec_ticker_to_cik.json')
    if not mapping_file.exists():
        raise RuntimeError('sec_ticker_to_cik.json was not downloaded.')

    raw_mapping = json.loads(mapping_file.read_text(encoding='utf-8'))
    if not isinstance(raw_mapping, dict):
        raise RuntimeError('sec_ticker_to_cik.json must contain an object.')

    by_ticker = {
        str(k).upper().strip(): str(v).zfill(10)
        for k, v in raw_mapping.items()
        if k and v
    }

    # Keep the last good data. A temporary SEC failure must not wipe the file.
    result = {}
    previous_failures = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding='utf-8'))
            if isinstance(previous, dict):
                if isinstance(previous.get('data'), dict):
                    result.update(previous['data'])
                if isinstance(previous.get('errors'), dict):
                    previous_failures.update(previous['errors'])
        except Exception as exc:
            print(f'WARNING: existing {OUT} could not be read: {exc}')

    requested = []
    for raw in tickers:
        ticker = str(raw).upper().strip()
        if not ticker or ticker in {x['ticker'] for x in requested}:
            continue
        cik = by_ticker.get(ticker)
        if not cik:
            print(f'SKIP {ticker}: no stock CIK mapping (ETF/fund/unsupported)')
            continue
        requested.append({'ticker': ticker, 'cik': cik})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(item):
        ticker = item['ticker']
        cik = item['cik']
        url = f'{COMMON}/api/xbrl/companyfacts/CIK{cik}.json'
        try:
            facts = get_json(url, DATA_HEADERS)
            record = build_company(ticker, cik, facts)
            clean = {
                k: v for k, v in record.items()
                if v is not None or k.startswith('__')
            }
            return ticker, clean, None
        except Exception as exc:
            return ticker, None, str(exc)

    # Four concurrent requests stays comfortably below SEC's 10 req/sec
    # fair-access ceiling while keeping a 30-stock portfolio reasonably fast.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_one, item) for item in requested]
        for future in as_completed(futures):
            ticker, record, error = future.result()
            if record is not None:
                result[ticker] = record
                previous_failures.pop(ticker, None)
                print(f'OK {ticker}')
            else:
                previous_failures[ticker] = error
                print(f'FAIL {ticker}: {error}')

    payload = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'SEC EDGAR XBRL Companyfacts',
        'data': result,
        'errors': previous_failures,
        'stats': {
            'requested_stock_tickers': len(requested),
            'successful_records': len(result),
            'failed_tickers': len(previous_failures),
        },
    }

    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8'
    )
    print(
        f"Wrote {OUT}: {len(result)} records, "
        f"{len(previous_failures)} failed/skipped records."
    )


if __name__ == '__main__':
    main()
