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
    'Portfolio Manager GitHub Actions 41898282+github-actions[bot]@users.noreply.github.com'
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
SEC_FILES = 'https://www.sec.gov/files'


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def get_json(url, headers, timeout=60, retries=4):
    last_error = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            status = r.status_code
            content_type = (r.headers.get('content-type') or '').lower()

            if status == 200:
                text = r.text.lstrip('\ufeff').strip()
                if not text:
                    raise RuntimeError(f'Empty SEC response: {url}')
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    preview = text[:180].replace('\n', ' ')
                    raise RuntimeError(
                        f'SEC returned non-JSON content (HTTP 200, content-type={content_type}). '
                        f'Preview: {preview}'
                    ) from e

            if status in (403, 429, 500, 502, 503, 504):
                retry_after = r.headers.get('Retry-After')
                try:
                    wait = float(retry_after) if retry_after else (2 ** attempt) * 2
                except Exception:
                    wait = (2 ** attempt) * 2
                last_error = RuntimeError(
                    f'SEC HTTP {status} for {url}; content-type={content_type}; '
                    f'body={r.text[:180].replace(chr(10), " ")}'
                )
                if attempt < retries - 1:
                    time.sleep(min(wait, 30))
                    continue
                raise last_error

            r.raise_for_status()

        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(min((2 ** attempt) * 2, 30))
                continue
            raise

    raise last_error or RuntimeError(f'Unable to fetch {url}')


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

    # This endpoint was returning non-JSON to the GitHub runner in the previous version.
    # Use a declared contact in SEC_USER_AGENT and validate the response before .json().
    mapping = get_json(f'{SEC_FILES}/company_tickers.json', HEADERS)
    by_ticker = {
        str(v['ticker']).upper(): str(v['cik_str']).zfill(10)
        for v in mapping.values()
        if v.get('ticker') and v.get('cik_str')
    }

    result = {}
    for ticker in tickers:
        t = str(ticker).upper().strip()
        cik = by_ticker.get(t)
        if not cik:
            print(f'SKIP {t}: CIK not found in SEC company_tickers.json')
            continue
        try:
            url = f'{COMMON}/api/xbrl/companyfacts/CIK{cik}.json'
            facts = get_json(url, DATA_HEADERS)
            result[t] = {k: v for k, v in build_company(t, cik, facts).items()
                         if v is not None or k.startswith('__')}
            print(f'OK {t} CIK={cik}')
            time.sleep(0.25)
        except Exception as e:
            print(f'FAIL {t}: {e}')

    payload = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'SEC EDGAR XBRL Companyfacts',
        'data': result,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'Wrote {OUT} with {len(result)} SEC records.')


if __name__ == '__main__':
    main()
