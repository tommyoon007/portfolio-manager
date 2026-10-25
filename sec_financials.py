import json
import os
import time
import math
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path('sec_financials.json')
TICKERS_FILE = Path('sec_tickers.json')

# SEC Fair Access: the User-Agent must identify the application and provide a real contact email.
CONTACT_EMAIL = (os.environ.get('SEC_CONTACT_EMAIL') or '').strip()
ENV_UA = (os.environ.get('SEC_USER_AGENT') or '').strip()
if CONTACT_EMAIL:
    UA = f'PortfolioManager/1.1 (contact: {CONTACT_EMAIL})'
elif '@' in ENV_UA:
    UA = ENV_UA
else:
    UA = ''

if not UA:
    raise RuntimeError(
        'SEC_CONTACT_EMAIL is missing. Add a repository Actions secret named '
        'SEC_CONTACT_EMAIL containing a real contact email, then rerun the workflow.'
    )

HEADERS = {
    'User-Agent': UA,
    'Accept': 'application/json',
    'Accept-Encoding': 'gzip, deflate',
}
COMMON = 'https://data.sec.gov'


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def get_json(url, timeout=(10, 25)):
    """Fetch SEC JSON with a clear failure and bounded retries for transient errors."""
    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                text = r.text.lstrip('\ufeff').strip()
                if not text:
                    raise RuntimeError(f'Empty SEC response: {url}')
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    preview = text[:160].replace('\n', ' ')
                    raise RuntimeError(
                        f'SEC returned non-JSON content for {url}; '
                        f'content-type={r.headers.get("content-type", "")}; preview={preview}'
                    ) from exc

            body = r.text[:180].replace('\n', ' ')
            last_error = RuntimeError(
                f'SEC HTTP {r.status_code} for {url}; '
                f'content-type={r.headers.get("content-type", "")}; body={body}'
            )
            # A 403 is usually a policy/contact/network block; retrying immediately won't help.
            if r.status_code not in (429, 500, 502, 503, 504):
                raise last_error
            retry_after = num(r.headers.get('Retry-After'))
            time.sleep(min(retry_after if retry_after is not None else (2 ** attempt), 8))
        except requests.RequestException as exc:
            last_error = RuntimeError(f'SEC request failed: {url} — {exc}')
            time.sleep(min(2 ** attempt, 8))
    raise last_error or RuntimeError(f'SEC request failed: {url}')


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
    # Prefer the latest-filed fact for the same quarter end.
    ded = {}
    for r in q:
        if r['end'] not in ded or r.get('filed', '') >= ded[r['end']].get('filed', ''):
            ded[r['end']] = r
    return sorted(ded.values(), key=lambda x: x['end'])


def ttm_from_quarters(arr):
    q = quarter_values(arr)
    if len(q) < 4:
        return None
    last4 = q[-4:]
    # Require four distinct quarter ends and roughly quarterly spacing.
    ends = [datetime.fromisoformat(x['end']) for x in last4]
    if len({x['end'] for x in last4}) != 4:
        return None
    gaps = [(ends[i] - ends[i - 1]).days for i in range(1, 4)]
    if not all(70 <= gap <= 120 for gap in gaps):
        return None
    return sum(x['val'] for x in last4)


def ttm_or_annual(arr):
    """Return 4-quarter TTM, or a genuine annual-duration fact; never label one quarter as TTM."""
    ttm = ttm_from_quarters(arr)
    if ttm is not None:
        return ttm
    annual = []
    for r in duration_facts(arr):
        try:
            days = (datetime.fromisoformat(r['end']) - datetime.fromisoformat(r['start'])).days
        except Exception:
            continue
        if 330 <= days <= 380:
            annual.append(r)
    return annual[-1]['val'] if annual else None


def latest_q_growth(arr):
    q = quarter_values(arr)
    if len(q) < 5:
        return None
    a, b = q[-1]['val'], q[-5]['val']
    return plausible_growth(a, b)


def fact_latest(arr):
    rows = instant_facts(arr)
    return rows[-1]['val'] if rows else None


def fact_first_latest(facts_all, taxonomies, tags, unit='USD', duration=False):
    """First usable concept from the preferred tag aliases and taxonomies."""
    for taxonomy in taxonomies:
        base = facts_all.get(taxonomy, {})
        for tag in tags:
            obj = base.get(tag, {})
            arr = obj.get('units', {}).get(unit, [])
            rows = duration_facts(arr) if duration else instant_facts(arr)
            if rows:
                return rows[-1]['val'], arr
    return None, []


def arr_for(facts_all, taxonomy_tags, unit='USD'):
    for taxonomy, tags in taxonomy_tags:
        base = facts_all.get(taxonomy, {})
        for tag in tags:
            arr = base.get(tag, {}).get('units', {}).get(unit, [])
            if arr:
                return arr
    return []


def plausible_ratio(numerator, denominator, lower=-100.0, upper=100.0):
    """Return a percentage only when the denominator is positive and the ratio is plausible."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = numerator / denominator * 100.0
    if not math.isfinite(value) or value < lower or value > upper:
        return None
    return value


def plausible_growth(current, prior, lower=-95.0, upper=500.0):
    """Reject growth rates that are mathematically possible but too unstable for ranking."""
    if current is None or prior is None or prior <= 0:
        return None
    value = (current / prior - 1.0) * 100.0
    if not math.isfinite(value) or value < lower or value > upper:
        return None
    return value


def build_company(ticker, cik, payload):
    facts_all = payload.get('facts', {})
    tax_tags = [
        ('us-gaap', [
            'RevenueFromContractWithCustomerExcludingAssessedTax',
            'RevenueFromContractWithCustomerIncludingAssessedTax',
            'Revenues', 'SalesRevenueNet', 'SalesRevenueGoodsNet',
        ]),
        ('ifrs-full', ['Revenue', 'RevenueFromContractsWithCustomers']),
    ]
    revenue_arr = arr_for(facts_all, tax_tags)
    ni_arr = arr_for(facts_all, [
        ('us-gaap', ['NetIncomeLoss', 'ProfitLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic']),
        ('ifrs-full', ['ProfitLoss', 'ProfitLossAttributableToOwnersOfParent']),
    ])
    ocf_arr = arr_for(facts_all, [
        ('us-gaap', ['NetCashProvidedByUsedInOperatingActivities']),
        ('ifrs-full', ['CashFlowsFromUsedInOperatingActivities']),
    ])
    capex_arr = arr_for(facts_all, [
        ('us-gaap', ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets']),
        ('ifrs-full', ['PurchaseOfPropertyPlantAndEquipment', 'PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities']),
    ])
    opinc_arr = arr_for(facts_all, [
        ('us-gaap', ['OperatingIncomeLoss']),
        ('ifrs-full', ['OperatingProfitLoss']),
    ])
    interest_arr = arr_for(facts_all, [
        ('us-gaap', ['InterestExpenseNonOperating', 'InterestExpenseNonOperatingAndOperating', 'InterestAndDebtExpense']),
        ('ifrs-full', ['FinanceCosts', 'InterestExpense']),
    ])
    da_arr = arr_for(facts_all, [
        ('us-gaap', ['DepreciationDepletionAndAmortization', 'DepreciationDepletionAndAmortizationPropertyPlantAndEquipment']),
        ('ifrs-full', ['DepreciationAndAmortisationExpense', 'DepreciationDepletionAndAmortisationPropertyPlantAndEquipment']),
    ])

    revenue_ttm = ttm_or_annual(revenue_arr)
    ni_ttm = ttm_or_annual(ni_arr)
    ocf_ttm = ttm_or_annual(ocf_arr)
    capex_ttm = ttm_or_annual(capex_arr)
    fcf = (ocf_ttm - abs(capex_ttm)) if ocf_ttm is not None and capex_ttm is not None else None
    opinc_ttm = ttm_or_annual(opinc_arr)
    interest_ttm = ttm_or_annual(interest_arr)
    da_ttm = ttm_or_annual(da_arr)
    ebitda = (opinc_ttm + abs(da_ttm)) if opinc_ttm is not None and da_ttm is not None else None

    assets, _ = fact_first_latest(facts_all, ['us-gaap'], ['Assets'])
    liabilities, _ = fact_first_latest(facts_all, ['us-gaap'], ['Liabilities'])
    equity, _ = fact_first_latest(facts_all, ['us-gaap'], [
        'StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'
    ])
    if equity is None:
        equity, _ = fact_first_latest(facts_all, ['ifrs-full'], ['Equity', 'EquityAttributableToOwnersOfParent'])

    cash, _ = fact_first_latest(facts_all, ['us-gaap'], [
        'CashAndCashEquivalentsAtCarryingValue',
        'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
    ])
    if cash is None:
        cash, _ = fact_first_latest(facts_all, ['ifrs-full'], ['CashAndCashEquivalents'])

    ca, _ = fact_first_latest(facts_all, ['us-gaap'], ['AssetsCurrent'])
    cl, _ = fact_first_latest(facts_all, ['us-gaap'], ['LiabilitiesCurrent'])
    if ca is None:
        ca, _ = fact_first_latest(facts_all, ['ifrs-full'], ['CurrentAssets'])
    if cl is None:
        cl, _ = fact_first_latest(facts_all, ['ifrs-full'], ['CurrentLiabilities'])

    # Select one alias per debt bucket to avoid double-counting synonymous tags.
    debt_current, _ = fact_first_latest(facts_all, ['us-gaap'], [
        'LongTermDebtAndFinanceLeaseObligationsCurrent', 'LongTermDebtCurrent', 'ShortTermBorrowings', 'CommercialPaper'
    ])
    debt_noncurrent, _ = fact_first_latest(facts_all, ['us-gaap'], [
        'LongTermDebtAndFinanceLeaseObligationsNoncurrent', 'LongTermDebtNoncurrent', 'LongTermDebt'
    ])
    if debt_current is None:
        debt_current, _ = fact_first_latest(facts_all, ['ifrs-full'], ['BorrowingsCurrent', 'LeaseLiabilitiesCurrent'])
    if debt_noncurrent is None:
        debt_noncurrent, _ = fact_first_latest(facts_all, ['ifrs-full'], ['BorrowingsNoncurrent', 'LeaseLiabilitiesNoncurrent'])
    debt = (debt_current or 0) + (debt_noncurrent or 0) if debt_current is not None or debt_noncurrent is not None else None

    shares, _ = fact_first_latest(facts_all, ['dei'], ['EntityCommonStockSharesOutstanding'], unit='shares')
    # Do not copy basic shares into diluted shares; those are not the same metric.
    diluted_shares = None

    # Cross-field consistency gate. A ratio outside this range usually signals mismatched
    # periods, a wrong XBRL concept, or a reporting anomaly. Do not guess which field is right.
    if revenue_ttm is not None and revenue_ttm > 0 and opinc_ttm is not None:
        raw_margin = opinc_ttm / revenue_ttm * 100.0
        if not math.isfinite(raw_margin) or raw_margin < -100.0 or raw_margin > 100.0:
            print(f'WARNING {ticker}: inconsistent operating income/revenue ratio ({raw_margin:.1f}%). Suppressing affected metrics.')
            revenue_ttm = None
            opinc_ttm = None
            fcf = None if ocf_ttm is None or capex_ttm is None else fcf
            ebitda = None
    if ni_ttm is not None and revenue_ttm is not None and revenue_ttm > 0:
        net_margin = ni_ttm / revenue_ttm * 100.0
        if not math.isfinite(net_margin) or net_margin < -100.0 or net_margin > 100.0:
            print(f'WARNING {ticker}: inconsistent net income/revenue ratio ({net_margin:.1f}%). Suppressing net income.')
            ni_ttm = None

    return {
        'RevenueTTM': revenue_ttm,
        'QuarterlyRevenueGrowthYOY': latest_q_growth(revenue_arr),
        'QuarterlyEarningsGrowthYOY': latest_q_growth(ni_arr),
        'OperatingIncomeTTM': opinc_ttm,
        'NetIncomeTTM': ni_ttm,
        'OperatingMarginTTM': plausible_ratio(opinc_ttm, revenue_ttm, -100.0, 100.0),
        'ReturnOnEquityTTM': plausible_ratio(ni_ttm, equity, -200.0, 200.0),
        'DebtToEquity': plausible_ratio(debt, equity, 0.0, 2000.0),
        'CurrentRatio': (ca / cl) if ca is not None and cl not in (None, 0) else None,
        'OperatingCashflow': ocf_ttm,
        'FreeCashFlowTTM': fcf,
        'EBITDA': ebitda,
        'InterestExpenseTTM': abs(interest_ttm) if interest_ttm is not None else None,
        'SharesOutstanding': shares,
        'DilutedSharesOutstanding': diluted_shares,
        'Beta': None,
        'ForwardPE': None,
        'PERatio': None,
        'PEGRatio': None,
        'PriceToSalesRatioTTM': None,
        'EVToEBITDA': None,
        'MarketCapitalization': None,
        'TotalDebt': debt,
        'Cash': cash,
        '__source': 'SEC EDGAR XBRL Companyfacts',
        '__cik': cik,
        '__updatedAt': datetime.now(timezone.utc).isoformat(),
    }


def main():
    tickers = json.loads(TICKERS_FILE.read_text(encoding='utf-8'))
    if not isinstance(tickers, list):
        raise RuntimeError('sec_tickers.json must contain a JSON array of ticker symbols.')

    # Build ticker -> CIK mapping automatically from the SEC's official file.
    # This removes the need to manually create sec_ticker_to_cik.json.
    mapping_file = Path('sec_ticker_to_cik.json')
    ticker_map_url = 'https://www.sec.gov/files/company_tickers.json'
    raw_tickers = get_json(ticker_map_url)
    by_ticker = {}
    for row in raw_tickers.values():
        ticker = str(row.get('ticker', '')).upper().strip()
        cik = row.get('cik_str')
        if ticker and cik:
            by_ticker[ticker] = str(cik).zfill(10)

    # Recent/new listings may not yet appear in the SEC ticker file.
    # Verify these CIKs again if the SEC assigns a new registrant in future.
    by_ticker.update({
        'GFUZ': '0002074850',
        'XRPN': '0002092592',
    })
    mapping_file.write_text(
        json.dumps(by_ticker, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f'Created {mapping_file} with {len(by_ticker)} ticker mappings.')

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
    seen = set()
    for raw in tickers:
        ticker = str(raw).upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        cik = by_ticker.get(ticker)
        if not cik:
            print(f'SKIP {ticker}: no stock CIK mapping (ETF/fund/unsupported)')
            continue
        requested.append({'ticker': ticker, 'cik': cik})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(item):
        ticker, cik = item['ticker'], item['cik']
        url = f'{COMMON}/api/xbrl/companyfacts/CIK{cik}.json'
        try:
            facts = get_json(url)
            record = build_company(ticker, cik, facts)
            clean = {k: v for k, v in record.items() if v is not None or k.startswith('__')}
            return ticker, clean, None
        except Exception as exc:
            return ticker, None, str(exc)

    fresh_successes = 0
    # Four requests in parallel remains below SEC's 10 requests/second limit.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_one, item) for item in requested]
        for future in as_completed(futures):
            ticker, record, error = future.result()
            if record is not None:
                result[ticker] = record
                previous_failures.pop(ticker, None)
                fresh_successes += 1
                print(f'OK {ticker}')
            else:
                previous_failures[ticker] = error
                print(f'FAIL {ticker}: {error}')

    # Never publish an empty file as if the scheduled refresh had succeeded.
    if fresh_successes == 0 and not result:
        raise RuntimeError(
            'SEC returned zero usable company records. Existing data was not overwritten. '
            'Check the SEC_CONTACT_EMAIL secret and the HTTP errors above.'
        )

    payload = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'SEC EDGAR XBRL Companyfacts',
        'data': result,
        'errors': previous_failures,
        'stats': {
            'requested_stock_tickers': len(requested),
            'fresh_successful_records': fresh_successes,
            'total_cached_records': len(result),
            'failed_tickers': len(previous_failures),
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f"Wrote {OUT}: {fresh_successes} fresh records, {len(result)} total records, {len(previous_failures)} failed tickers.")


if __name__ == '__main__':
    main()
