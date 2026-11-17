import json
import os
import re
import time
import math
from datetime import datetime, timezone, timedelta
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


def is_annual_fact(row):
    try:
        days = (
            datetime.fromisoformat(row['end']) - datetime.fromisoformat(row['start'])
        ).days
    except Exception:
        return False
    return 330 <= days <= 380


def to_date(value):
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def ttm_from_quarters_with_basis(arr):
    q = quarter_values(arr)
    if len(q) < 4:
        return None, None
    last4 = q[-4:]
    ends = [to_date(x['end']) for x in last4]
    if any(e is None for e in ends):
        return None, None
    if len({x['end'] for x in last4}) != 4:
        return None, None
    gaps = [(ends[i] - ends[i - 1]).days for i in range(1, 4)]
    if not all(70 <= gap <= 120 for gap in gaps):
        return None, None
    return sum(x['val'] for x in last4), last4[-1]['end']


def ttm_from_annual_plus_ytd(arr):
    """TTM = 최근 확정 연간 실적 + 당해 누적(YTD) - 전년 같은 기간 누적.

    분기 4개가 연속으로 잡히지 않는 종목(당해 분기 3개만 공시 등)에서도 1년 전
    연간값으로 되돌아가지 않고 최근 12개월 실적을 만듭니다. 첫 회계연도가
    단축기간(창업 첫 해)인 종목도 같은 방식으로 계산합니다.
    """
    rows = duration_facts(arr)

    # 기준 기간 후보: 60~380일 기간. 최근 기간말 → 긴 기간 순으로 시도합니다.
    bases = []
    for r in rows:
        start, end = to_date(r['start']), to_date(r['end'])
        if start is None or end is None:
            continue
        days = (end - start).days
        if 60 <= days <= 380:
            bases.append((end, days, r))
    if not bases:
        return None, None
    bases.sort(key=lambda b: (b[0], b[1]), reverse=True)

    # 가장 최근에 보고된 기간까지 닿는 TTM만 인정합니다(오래된 기간의 TTM을 최신처럼 쓰지 않음).
    latest_end = max(end for end, _days, _r in bases)

    for fy_end, _days, base in bases:
        fy_start = to_date(base['start'])
        if fy_start is None:
            continue

        # 당해 누적: 기준 기간 다음 날부터 시작해 그 이후에 끝나는 기간
        current = []
        for r in rows:
            start, end = to_date(r['start']), to_date(r['end'])
            if start is None or end is None:
                continue
            if end <= fy_end:
                continue
            if abs((start - fy_end).days) > 10:
                continue
            if (end - start).days > 380:
                continue
            current.append(r)
        if not current:
            continue
        cur = max(current, key=lambda r: (r['end'], r.get('filed') or ''))
        cur_end = to_date(cur['end'])
        if cur_end is None or cur_end != latest_end:
            continue

        # 전년 같은 기간 누적: 같은 회계연도 시작일에서 출발해 1년 전 같은 날에 끝나는 기간
        try:
            target = cur_end.replace(year=cur_end.year - 1)
        except ValueError:  # 2월 29일
            target = cur_end.replace(year=cur_end.year - 1, day=28)

        prior = []
        for r in rows:
            start, end = to_date(r['start']), to_date(r['end'])
            if start is None or end is None:
                continue
            if end >= fy_end:
                continue
            if abs((start - fy_start).days) > 10:
                continue
            if abs((end - target).days) > 20:
                continue
            prior.append(r)
        if not prior:
            continue

        prev = min(prior, key=lambda r: abs((to_date(r['end']) - target).days))
        return base['val'] + cur['val'] - prev['val'], cur['end']

    return None, None


def ttm_with_basis(arr):
    """(TTM 값, 기준일)을 돌려줍니다. TTM을 만들 수 없으면 None."""
    for builder in (ttm_from_quarters_with_basis, ttm_from_annual_plus_ytd):
        value, basis = builder(arr)
        if value is not None:
            return value, basis
    annuals = [r for r in duration_facts(arr) if is_annual_fact(r)]
    if annuals:
        return annuals[-1]['val'], annuals[-1]['end']
    return None, None


def ttm_or_annual(arr):
    """Return 4-quarter TTM, or a genuine annual-duration fact; never label one quarter as TTM."""
    return ttm_with_basis(arr)[0]


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
    """First usable concept from the preferred tag aliases and taxonomies.

    Returns (value, raw array, basis date).
    """
    for taxonomy in taxonomies:
        base = facts_all.get(taxonomy, {})
        for tag in tags:
            obj = base.get(tag, {})
            arr = obj.get('units', {}).get(unit, [])
            rows = duration_facts(arr) if duration else instant_facts(arr)
            if rows:
                return rows[-1]['val'], arr, rows[-1].get('end')
    return None, [], None


def arr_for(facts_all, taxonomy_tags, unit='USD'):
    """Pick the alias tag that actually holds the most recent data.

    태그 목록 순서대로 첫 번째 비어있지 않은 값을 쓰면, 기업이 새 태그로
    보고를 옮긴 뒤에도 옛 태그(수년 전 값)가 선택될 수 있습니다. 그래서
    가장 최근 end(같으면 최근 filed)를 가진 태그를 고르고, 날짜가 같으면
    목록에 먼저 적힌 별칭을 우선합니다.
    """
    candidates = []
    for taxonomy_order, (taxonomy, tags) in enumerate(taxonomy_tags):
        base = facts_all.get(taxonomy, {})
        for tag_order, tag in enumerate(tags):
            arr = base.get(tag, {}).get('units', {}).get(unit, [])
            if not arr:
                continue
            ends = [
                r['end'] for r in arr
                if r.get('end') and num(r.get('val')) is not None
            ]
            if not ends:
                continue
            latest_end = max(ends)
            latest_filed = max(
                (str(r.get('filed') or '') for r in arr
                 if r.get('end') == latest_end and num(r.get('val')) is not None),
                default='',
            )
            candidates.append(((latest_end, latest_filed, -taxonomy_order, -tag_order), arr))
    if not candidates:
        return []
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


# 이보다 오래된 재무 데이터는 현재 실적으로 쓰지 않고 결측 처리합니다.
MAX_DATA_AGE_DAYS = 400


def days_old(end):
    value = to_date(end)
    if value is None:
        return None
    return (datetime.now(timezone.utc).date() - value).days


def fresh_ttm(ticker, label, arr):
    """TTM 값과 기준일. 400일 이상 지난 데이터는 경고 후 결측 처리합니다."""
    value, basis = ttm_with_basis(arr)
    if value is None:
        return None, None
    age = days_old(basis)
    if age is not None and age > MAX_DATA_AGE_DAYS:
        print(
            f'WARNING {ticker}: {label}의 최근 기준일 {basis}이 {age}일 지나 '
            f'{MAX_DATA_AGE_DAYS}일 기준을 넘었습니다. 결측 처리합니다.'
        )
        return None, None
    return value, basis


def latest_shares(facts_all):
    """발행주식수: dei → us-gaap 보통주 → 가중평균 기본주식수 순으로, 가장 최근 값 사용.

    가중평균 주식수는 기간 사실(duration)이라서 분기(3개월) 값과 누적(9개월) 값이
    같은 기준일에 함께 있습니다. 그때는 현재 발행주식수에 가장 가까운 최근 분기를 씁니다.
    """
    chain = [
        ('dei', 'EntityCommonStockSharesOutstanding'),
        ('us-gaap', 'CommonStockSharesOutstanding'),
        ('us-gaap', 'WeightedAverageNumberOfSharesOutstandingBasic'),
    ]
    best = None
    for order, (taxonomy, tag) in enumerate(chain):
        arr = facts_all.get(taxonomy, {}).get(tag, {}).get('units', {}).get('shares', [])
        rows = [r for r in arr if r.get('end') and num(r.get('val')) is not None]
        if not rows:
            continue

        def shape(r):
            start, end = to_date(r.get('start')), to_date(r['end'])
            return (end - start).days if start and end else 0

        latest = max(rows, key=lambda r: (r['end'], -shape(r), str(r.get('filed') or '')))
        # 최근 기준일 우선, 날짜가 같으면 체인 앞쪽 태그 우선
        rank = (latest['end'], -order)
        if best is None or rank > best[0]:
            best = (rank, latest['val'], f'{taxonomy}:{tag}', latest['end'])
    if best is None:
        return None, None, None
    return best[1], best[2], best[3]


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

    revenue_ttm, revenue_asof = fresh_ttm(ticker, '매출', revenue_arr)
    ni_ttm, ni_asof = fresh_ttm(ticker, '순이익', ni_arr)
    ocf_ttm, ocf_asof = fresh_ttm(ticker, '영업현금흐름', ocf_arr)
    capex_ttm, capex_asof = fresh_ttm(ticker, '설비투자', capex_arr)
    fcf = (ocf_ttm - abs(capex_ttm)) if ocf_ttm is not None and capex_ttm is not None else None
    opinc_ttm, opinc_asof = fresh_ttm(ticker, '영업이익', opinc_arr)
    interest_ttm, interest_asof = fresh_ttm(ticker, '이자비용', interest_arr)
    da_ttm, da_asof = fresh_ttm(ticker, '감가상각', da_arr)
    ebitda = (opinc_ttm + abs(da_ttm)) if opinc_ttm is not None and da_ttm is not None else None

    # 기록에 남길 실제 재무 기준일 (사용한 값들 중 가장 최근 기간말)
    basis_dates = [
        d for d in (
            revenue_asof, ni_asof, ocf_asof, capex_asof,
            opinc_asof, interest_asof, da_asof,
        ) if d
    ]

    assets, _, assets_asof = fact_first_latest(facts_all, ['us-gaap'], ['Assets'])
    liabilities, _, liabilities_asof = fact_first_latest(facts_all, ['us-gaap'], ['Liabilities'])
    equity, _, equity_asof = fact_first_latest(facts_all, ['us-gaap'], [
        'StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'
    ])
    if equity is None:
        equity, _, equity_asof = fact_first_latest(facts_all, ['ifrs-full'], ['Equity', 'EquityAttributableToOwnersOfParent'])

    cash, _, cash_asof = fact_first_latest(facts_all, ['us-gaap'], [
        'CashAndCashEquivalentsAtCarryingValue',
        'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
    ])
    if cash is None:
        cash, _, cash_asof = fact_first_latest(facts_all, ['ifrs-full'], ['CashAndCashEquivalents'])

    ca, _, ca_asof = fact_first_latest(facts_all, ['us-gaap'], ['AssetsCurrent'])
    cl, _, cl_asof = fact_first_latest(facts_all, ['us-gaap'], ['LiabilitiesCurrent'])
    if ca is None:
        ca, _, ca_asof = fact_first_latest(facts_all, ['ifrs-full'], ['CurrentAssets'])
    if cl is None:
        cl, _, cl_asof = fact_first_latest(facts_all, ['ifrs-full'], ['CurrentLiabilities'])

    basis_dates.extend(
        d for d in (assets_asof, liabilities_asof, equity_asof, cash_asof, ca_asof, cl_asof) if d
    )

    # Select one alias per debt bucket to avoid double-counting synonymous tags.
    debt_current, _, debt_current_asof = fact_first_latest(facts_all, ['us-gaap'], [
        'LongTermDebtAndFinanceLeaseObligationsCurrent', 'LongTermDebtCurrent', 'ShortTermBorrowings', 'CommercialPaper'
    ])
    debt_noncurrent, _, debt_noncurrent_asof = fact_first_latest(facts_all, ['us-gaap'], [
        'LongTermDebtAndFinanceLeaseObligationsNoncurrent', 'LongTermDebtNoncurrent', 'LongTermDebt'
    ])
    if debt_current is None:
        debt_current, _, debt_current_asof = fact_first_latest(facts_all, ['ifrs-full'], ['BorrowingsCurrent', 'LeaseLiabilitiesCurrent'])
    if debt_noncurrent is None:
        debt_noncurrent, _, debt_noncurrent_asof = fact_first_latest(facts_all, ['ifrs-full'], ['BorrowingsNoncurrent', 'LeaseLiabilitiesNoncurrent'])
    debt = (debt_current or 0) + (debt_noncurrent or 0) if debt_current is not None or debt_noncurrent is not None else None
    basis_dates.extend(d for d in (debt_current_asof, debt_noncurrent_asof) if d)

    # 발행주식수: dei 태그가 없는 기업은 us-gaap 대체 태그로 보완합니다.
    shares, shares_source, shares_asof = latest_shares(facts_all)
    if shares is not None and shares_source != 'dei:EntityCommonStockSharesOutstanding':
        print(f'NOTE {ticker}: 발행주식수를 {shares_source} 기준일 {shares_asof} 값으로 보완했습니다.')
    if shares is None:
        print(f'WARNING {ticker}: 발행주식수를 찾지 못했습니다.')
    # Do not copy basic shares into diluted shares; those are not the same metric.
    diluted_shares = None

    # Cross-field consistency gate. 극단적인 비율은 기간이 어긋났거나 일회성 손실이 큰 경우를
    # 모두 포함하므로, 매출 자체는 남기고 그 비율로만 만들어지는 지표만 제거합니다.
    if revenue_ttm is not None and revenue_ttm > 0 and opinc_ttm is not None:
        raw_margin = opinc_ttm / revenue_ttm * 100.0
        if not math.isfinite(raw_margin) or raw_margin < -100.0 or raw_margin > 100.0:
            print(f'WARNING {ticker}: 영업이익/매출 비율이 {raw_margin:.1f}%로 비정상입니다. 영업이익 지표만 비웁니다(매출은 유지).')
            opinc_ttm = None
            ebitda = None
    if ni_ttm is not None and revenue_ttm is not None and revenue_ttm > 0:
        net_margin = ni_ttm / revenue_ttm * 100.0
        if not math.isfinite(net_margin) or net_margin < -100.0 or net_margin > 100.0:
            print(f'WARNING {ticker}: 순이익/매출 비율이 {net_margin:.1f}%입니다. 순이익 금액은 유지하고 비율 지표만 해석하지 않습니다.')

    as_of = max(basis_dates) if basis_dates else None
    if as_of is None:
        print(f'WARNING {ticker}: 재무 기준일을 확인할 수 없습니다.')
    else:
        age = days_old(as_of)
        if age is not None and age > MAX_DATA_AGE_DAYS:
            print(f'WARNING {ticker}: 재무 데이터 기준일 {as_of}이 {age}일 전입니다.')
        if shares_asof and str(shares_asof) < str(as_of):
            print(f'NOTE {ticker}: 발행주식수 기준일 {shares_asof}이 재무 기준일 {as_of}보다 앞섭니다.')

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
        '__asOf': as_of,
        '__sharesAsOf': shares_asof,
        '__sharesSource': shares_source,
        '__updatedAt': datetime.now(timezone.utc).isoformat(),
    }


def load_etf_holding_tickers():
    """etf_holdings.json에 있는 SMH/SOXX/ROBO 편입종목 티커의 합집합."""
    path = Path('etf_holdings.json')
    if not path.exists():
        print('WARNING: etf_holdings.json이 없어 ETF 편입종목은 건너뜁니다.')
        return []
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'WARNING: etf_holdings.json을 읽지 못했습니다: {exc}')
        return []
    data = doc.get('data') if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        return []
    out = []
    seen = set()
    for etf in ('SMH', 'SOXX', 'ROBO'):
        record = data.get(etf)
        if not isinstance(record, dict):
            continue
        for holding in record.get('holdings') or []:
            if not isinstance(holding, dict):
                continue
            ticker = str(holding.get('ticker') or '').upper().strip()
            if not ticker or ticker in seen:
                continue
            if not re.fullmatch(r'[A-Z0-9.\-]{1,10}', ticker):
                continue
            seen.add(ticker)
            out.append(ticker)
    return out


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
        requested.append({'ticker': ticker, 'cik': cik, 'source': 'sec_tickers'})
    stock_requested = len(requested)

    # ETF(SMH/SOXX/ROBO) 편입종목도 함께 최신화합니다.
    # 보유 종목이 아니어도 ETF 점수 계산이 편입종목 재무를 가중평균하므로,
    # 편입목록에 있는 미국 상장 종목까지 CIK를 매핑해 같이 조회합니다.
    etf_tickers = load_etf_holding_tickers()
    etf_added = 0
    etf_unmapped = []
    for ticker in etf_tickers:
        if ticker in seen:
            continue
        cik = by_ticker.get(ticker)
        if not cik:
            etf_unmapped.append(ticker)
            continue
        seen.add(ticker)
        requested.append({'ticker': ticker, 'cik': cik, 'source': 'etf_holdings'})
        etf_added += 1
    if etf_tickers:
        print(
            f'ETF 편입종목: {len(etf_tickers)}개 확인, {etf_added}개 CIK 매핑 추가, '
            f'{len(etf_unmapped)}개 매핑 불가(해외/비상장/ETF)'
        )
        if etf_unmapped:
            print('  매핑 불가: ' + ', '.join(sorted(etf_unmapped)))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(item):
        ticker, cik = item['ticker'], item['cik']
        url = f'{COMMON}/api/xbrl/companyfacts/CIK{cik}.json'
        try:
            time.sleep(0.15)  # SEC 초당 10회 제한을 넘지 않도록 간격을 둡니다.
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
            'requested_stock_tickers': stock_requested,
            'requested_etf_holding_tickers': etf_added,
            'etf_holdings_tickers_without_cik': len(etf_unmapped),
            'fresh_successful_records': fresh_successes,
            'total_cached_records': len(result),
            'failed_tickers': len(previous_failures),
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f"Wrote {OUT}: {fresh_successes} fresh records, {len(result)} total records, {len(previous_failures)} failed tickers.")


if __name__ == '__main__':
    main()
