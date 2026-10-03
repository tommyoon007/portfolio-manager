import json
import os
import time
import math
from datetime import datetime, timezone
from pathlib import Path

import requests


OUT = Path('sec_financials.json')
TICKERS_FILE = Path('sec_tickers.json')

# ============================================================
# SEC CONTACT / USER-AGENT
# ============================================================

contact_email = os.environ.get('SEC_CONTACT_EMAIL', '').strip()

if not contact_email:
    raise RuntimeError(
        'SEC_CONTACT_EMAIL 환경변수가 없습니다. '
        'GitHub Repository Secret에 SEC_CONTACT_EMAIL을 추가하세요.'
    )

# SEC는 자동화 요청에 명확한 User-Agent와 연락처를 요구한다.
UA = (
    f'PortfolioManager/1.0 '
    f'{contact_email}'
)

HEADERS = {
    'User-Agent': UA,
    'Accept': 'application/json',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

DATA_HEADERS = dict(HEADERS)

COMMON = 'https://data.sec.gov'


# ============================================================
# BASIC HELPERS
# ============================================================

def num(v):
    try:
        x = float(v)

        if math.isfinite(x):
            return x

        return None

    except Exception:
        return None


def get_json(url, headers, timeout=(15, 30)):
    """
    SEC JSON 요청.

    - 명확한 User-Agent 사용
    - Host 헤더는 직접 지정하지 않음
    - 순차 요청을 전제로 사용
    - 일시적 네트워크 오류는 짧게 재시도
    - 403은 SEC 차단 여부를 명확하게 표시
    """

    last_error = None

    for attempt in range(2):

        try:

            r = requests.get(
                url,
                headers=headers,
                timeout=timeout
            )

        except requests.RequestException as exc:

            last_error = (
                f'SEC request failed: {exc}'
            )

            if attempt == 0:
                time.sleep(3)
                continue

            raise RuntimeError(
                f'{last_error}'
            ) from exc

        content_type = (
            r.headers.get('content-type')
            or ''
        ).lower()

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if r.status_code == 200:

            text = (
                r.text
                .lstrip('\ufeff')
                .strip()
            )

            if not text:
                raise RuntimeError(
                    f'Empty SEC response: {url}'
                )

            try:
                return json.loads(text)

            except json.JSONDecodeError as exc:

                preview = (
                    text[:300]
                    .replace('\n', ' ')
                )

                raise RuntimeError(
                    'SEC returned non-JSON content '
                    f'for {url}; '
                    f'content-type={content_type}; '
                    f'preview={preview}'
                ) from exc

        # ----------------------------------------------------
        # SEC AUTOMATED TOOL BLOCK
        # ----------------------------------------------------

        if r.status_code == 403:

            body = (
                r.text[:500]
                .replace('\n', ' ')
            )

            raise RuntimeError(
                'SEC HTTP 403 — automated access blocked. '
                f'URL={url}; '
                f'content-type={content_type}; '
                f'body={body}'
            )

        # ----------------------------------------------------
        # RATE LIMIT
        # ----------------------------------------------------

        if r.status_code in (429, 503):

            body = (
                r.text[:300]
                .replace('\n', ' ')
            )

            last_error = (
                f'SEC HTTP {r.status_code} for {url}; '
                f'content-type={content_type}; '
                f'body={body}'
            )

            if attempt == 0:
                time.sleep(5)
                continue

            raise RuntimeError(last_error)

        # ----------------------------------------------------
        # OTHER HTTP ERROR
        # ----------------------------------------------------

        body = (
            r.text[:300]
            .replace('\n', ' ')
        )

        raise RuntimeError(
            f'SEC HTTP {r.status_code} for {url}; '
            f'content-type={content_type}; '
            f'body={body}'
        )

    raise RuntimeError(
        last_error or
        f'Unknown SEC request error: {url}'
    )


# ============================================================
# XBRL FACT HELPERS
# ============================================================

def duration_facts(arr):

    out = []

    for x in arr or []:

        if (
            x.get('start')
            and x.get('end')
            and num(x.get('val')) is not None
        ):

            y = dict(x)
            y['val'] = num(x['val'])

            out.append(y)

    return sorted(
        out,
        key=lambda x: (
            x.get('end', ''),
            x.get('filed', '')
        )
    )


def instant_facts(arr):

    out = []

    for x in arr or []:

        if (
            not x.get('start')
            and num(x.get('val')) is not None
        ):

            y = dict(x)
            y['val'] = num(x['val'])

            out.append(y)

    return sorted(
        out,
        key=lambda x: (
            x.get('end', ''),
            x.get('filed', '')
        )
    )


def quarter_values(arr):

    q = []

    for r in duration_facts(arr):

        try:

            start = datetime.fromisoformat(
                r['start']
            )

            end = datetime.fromisoformat(
                r['end']
            )

            days = (
                end - start
            ).days

        except Exception:

            continue

        if 70 <= days <= 120:
            q.append(r)

    ded = {}

    for r in q:
        ded[r['end']] = r

    return sorted(
        ded.values(),
        key=lambda x: x['end']
    )


def ttm_from_quarters(arr):

    q = quarter_values(arr)

    if len(q) >= 4:

        return sum(
            x['val']
            for x in q[-4:]
        )

    return None


def latest_q_growth(arr):

    q = quarter_values(arr)

    if len(q) < 5:
        return None

    a = q[-1]['val']
    b = q[-5]['val']

    if b == 0:
        return None

    return (
        (a / b - 1)
        * 100
    )


def fact_latest(arr):

    rows = instant_facts(arr)

    return (
        rows[-1]['val']
        if rows
        else None
    )


def fact_duration_latest(arr):

    rows = duration_facts(arr)

    return (
        rows[-1]['val']
        if rows
        else None
    )


def arr_for(
    facts_all,
    taxonomy,
    tags,
    unit
):

    base = facts_all.get(
        taxonomy,
        {}
    )

    for tag in tags:

        obj = base.get(
            tag,
            {}
        )

        arr = (
            obj
            .get('units', {})
            .get(unit, [])
        )

        if arr:
            return arr

    return []


# ============================================================
# BUILD COMPANY DATA
# ============================================================

def build_company(
    ticker,
    cik,
    payload
):

    facts_all = payload.get(
        'facts',
        {}
    )

    # --------------------------------------------------------
    # REVENUE
    # --------------------------------------------------------

    revenue_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'RevenueFromContractWithCustomerExcludingAssessedTax',
            'Revenues',
            'SalesRevenueNet'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # NET INCOME
    # --------------------------------------------------------

    ni_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'NetIncomeLoss',
            'ProfitLoss',
            'NetIncomeLossAvailableToCommonStockholdersBasic'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # OPERATING CASH FLOW
    # --------------------------------------------------------

    ocf_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'NetCashProvidedByUsedInOperatingActivities'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # CAPEX
    # --------------------------------------------------------

    capex_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'PaymentsToAcquirePropertyPlantAndEquipment',
            'PaymentsToAcquireProductiveAssets'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # OPERATING INCOME
    # --------------------------------------------------------

    opinc_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'OperatingIncomeLoss'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # INTEREST EXPENSE
    # --------------------------------------------------------

    interest_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'InterestExpenseNonOperating',
            'InterestExpenseNonOperatingAndOperating'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # DEPRECIATION / AMORTIZATION
    # --------------------------------------------------------

    da_arr = arr_for(
        facts_all,
        'us-gaap',
        [
            'DepreciationDepletionAndAmortization',
            'DepreciationDepletionAndAmortizationPropertyPlantAndEquipment'
        ],
        'USD'
    )

    # --------------------------------------------------------
    # TTM
    # --------------------------------------------------------

    revenue_ttm = (
        ttm_from_quarters(
            revenue_arr
        )
        or fact_duration_latest(
            revenue_arr
        )
    )

    ni_ttm = (
        ttm_from_quarters(
            ni_arr
        )
        or fact_duration_latest(
            ni_arr
        )
    )

    ocf_ttm = (
        ttm_from_quarters(
            ocf_arr
        )
        or fact_duration_latest(
            ocf_arr
        )
    )

    capex_ttm = (
        ttm_from_quarters(
            capex_arr
        )
        or fact_duration_latest(
            capex_arr
        )
    )

    # --------------------------------------------------------
    # FREE CASH FLOW
    # --------------------------------------------------------

    fcf = (
        ocf_ttm - abs(capex_ttm)
        if (
            ocf_ttm is not None
            and capex_ttm is not None
        )
        else None
    )

    # --------------------------------------------------------
    # OPERATING INCOME
    # --------------------------------------------------------

    opinc_ttm = (
        ttm_from_quarters(
            opinc_arr
        )
        or fact_duration_latest(
            opinc_arr
        )
    )

    # --------------------------------------------------------
    # INTEREST
    # --------------------------------------------------------

    interest_ttm = (
        ttm_from_quarters(
            interest_arr
        )
        or fact_duration_latest(
            interest_arr
        )
    )

    # --------------------------------------------------------
    # D&A
    # --------------------------------------------------------

    da_ttm = (
        ttm_from_quarters(
            da_arr
        )
        or fact_duration_latest(
            da_arr
        )
    )

    # --------------------------------------------------------
    # EBITDA
    # --------------------------------------------------------

    ebitda = (
        opinc_ttm + abs(da_ttm)
        if (
            opinc_ttm is not None
            and da_ttm is not None
        )
        else None
    )

    # --------------------------------------------------------
    # BALANCE SHEET
    # --------------------------------------------------------

    us = facts_all.get(
        'us-gaap',
        {}
    )

    assets = fact_latest(
        us.get('Assets', {})
        .get('units', {})
        .get('USD', [])
    )

    liabilities = fact_latest(
        us.get('Liabilities', {})
        .get('units', {})
        .get('USD', [])
    )

    # --------------------------------------------------------
    # EQUITY
    # --------------------------------------------------------

    equity = None

    for tag in [
        'StockholdersEquity',
        'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'
    ]:

        equity = fact_latest(
            us.get(tag, {})
            .get('units', {})
            .get('USD', [])
        )

        if equity is not None:
            break

    # --------------------------------------------------------
    # CASH
    # --------------------------------------------------------

    cash = None

    for tag in [
        'CashAndCashEquivalentsAtCarryingValue',
        'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
    ]:

        cash = fact_latest(
            us.get(tag, {})
            .get('units', {})
            .get('USD', [])
        )

        if cash is not None:
            break

    # --------------------------------------------------------
    # CURRENT ASSETS / LIABILITIES
    # --------------------------------------------------------

    ca = fact_latest(
        us.get('AssetsCurrent', {})
        .get('units', {})
        .get('USD', [])
    )

    cl = fact_latest(
        us.get('LiabilitiesCurrent', {})
        .get('units', {})
        .get('USD', [])
    )

    # --------------------------------------------------------
    # DEBT
    # --------------------------------------------------------

    debt = 0

    for tag in [
        'LongTermDebtAndFinanceLeaseObligationsCurrent',
        'LongTermDebtCurrent',
        'LongTermDebtNoncurrent',
        'LongTermDebtAndFinanceLeaseObligationsNoncurrent'
    ]:

        v = fact_latest(
            us.get(tag, {})
            .get('units', {})
            .get('USD', [])
        )

        if v is not None:
            debt += max(0, v)

    # --------------------------------------------------------
    # SHARES
    # --------------------------------------------------------

    dei = facts_all.get(
        'dei',
        {}
    )

    shobj = (
        dei
        .get(
            'EntityCommonStockSharesOutstanding',
            {}
        )
        .get('units', {})
        .get('shares', [])
    )

    shares_rows = instant_facts(
        shobj
    )

    shares = (
        shares_rows[-1]['val']
        if shares_rows
        else None
    )

    # --------------------------------------------------------
    # RETURN
    # --------------------------------------------------------

    return {

        'RevenueTTM':
            revenue_ttm,

        'QuarterlyRevenueGrowthYOY':
            latest_q_growth(
                revenue_arr
            ),

        'QuarterlyEarningsGrowthYOY':
            latest_q_growth(
                ni_arr
            ),

        'OperatingIncomeTTM':
            opinc_ttm,

        'NetIncomeTTM':
            ni_ttm,

        'OperatingMarginTTM':
            (
                opinc_ttm
                / revenue_ttm
                * 100
                if (
                    opinc_ttm is not None
                    and revenue_ttm
                )
                else None
            ),

        'ReturnOnEquityTTM':
            (
                ni_ttm
                / equity
                * 100
                if (
                    ni_ttm is not None
                    and equity not in (
                        None,
                        0
                    )
                )
                else None
            ),

        'DebtToEquity':
            (
                debt
                / equity
                * 100
                if equity not in (
                    None,
                    0
                )
                else None
            ),

        'CurrentRatio':
            (
                ca / cl
                if (
                    ca is not None
                    and cl not in (
                        None,
                        0
                    )
                )
                else None
            ),

        'OperatingCashflow':
            ocf_ttm,

        'FreeCashFlowTTM':
            fcf,

        'EBITDA':
            ebitda,

        'InterestExpenseTTM':
            (
                abs(interest_ttm)
                if interest_ttm is not None
                else None
            ),

        'SharesOutstanding':
            shares,

        'DilutedSharesOutstanding':
            shares,

        'Beta':
            None,

        'ForwardPE':
            None,

        'PERatio':
            None,

        'PEGRatio':
            None,

        'PriceToSalesRatioTTM':
            None,

        'EVToEBITDA':
            None,

        'MarketCapitalization':
            None,

        'TotalDebt':
            debt,

        'Cash':
            cash,

        '__source':
            'SEC EDGAR XBRL companyfacts',

        '__cik':
            cik,

        '__updatedAt':
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # TICKERS
    # --------------------------------------------------------

    tickers = json.loads(
        TICKERS_FILE.read_text(
            encoding='utf-8'
        )
    )

    if not isinstance(
        tickers,
        list
    ):

        raise RuntimeError(
            'sec_tickers.json must contain '
            'a JSON array of ticker symbols.'
        )

    # --------------------------------------------------------
    # CIK MAPPING
    # --------------------------------------------------------

    mapping_file = Path(
        'sec_ticker_to_cik.json'
    )

    if not mapping_file.exists():

        raise RuntimeError(
            'sec_ticker_to_cik.json '
            'was not downloaded.'
        )

    raw_mapping = json.loads(
        mapping_file.read_text(
            encoding='utf-8'
        )
    )

    if not isinstance(
        raw_mapping,
        dict
    ):

        raise RuntimeError(
            'sec_ticker_to_cik.json '
            'must contain an object.'
        )

    by_ticker = {

        str(k).upper().strip():
            str(v).zfill(10)

        for k, v in raw_mapping.items()

        if k and v
    }

    # --------------------------------------------------------
    # KEEP PREVIOUS GOOD DATA
    # --------------------------------------------------------

    result = {}
    previous_failures = {}

    if OUT.exists():

        try:

            previous = json.loads(
                OUT.read_text(
                    encoding='utf-8'
                )
            )

            if isinstance(
                previous,
                dict
            ):

                if isinstance(
                    previous.get('data'),
                    dict
                ):

                    result.update(
                        previous['data']
                    )

                if isinstance(
                    previous.get('errors'),
                    dict
                ):

                    previous_failures.update(
                        previous['errors']
                    )

        except Exception as exc:

            print(
                f'WARNING: existing {OUT} '
                f'could not be read: {exc}'
            )

    # --------------------------------------------------------
    # BUILD REQUEST LIST
    # --------------------------------------------------------

    requested = []

    existing_tickers = set()

    for raw in tickers:

        ticker = (
            str(raw)
            .upper()
            .strip()
        )

        if (
            not ticker
            or ticker in existing_tickers
        ):
            continue

        cik = by_ticker.get(
            ticker
        )

        if not cik:

            print(
                f'SKIP {ticker}: '
                'no stock CIK mapping '
                '(ETF/fund/unsupported)'
            )

            continue

        requested.append({
            'ticker': ticker,
            'cik': cik
        })

        existing_tickers.add(
            ticker
        )

    # --------------------------------------------------------
    # FETCH ONE COMPANY
    # --------------------------------------------------------

    def fetch_one(item):

        ticker = item['ticker']
        cik = item['cik']

        url = (
            f'{COMMON}/api/xbrl/'
            f'companyfacts/CIK{cik}.json'
        )

        print(
            f'FETCH {ticker} '
            f'CIK={cik}'
        )

        try:

            facts = get_json(
                url,
                DATA_HEADERS
            )

            record = build_company(
                ticker,
                cik,
                facts
            )

            clean = {

                k: v

                for k, v in record.items()

                if (
                    v is not None
                    or k.startswith('__')
                )
            }

            return (
                ticker,
                clean,
                None
            )

        except Exception as exc:

            return (
                ticker,
                None,
                str(exc)
            )

    # --------------------------------------------------------
    # IMPORTANT:
    # SEC REQUESTS ARE NOW SEQUENTIAL.
    #
    # We deliberately do NOT use ThreadPoolExecutor.
    # This reduces simultaneous automated requests.
    # --------------------------------------------------------

    successful_this_run = 0

    for item in requested:

        ticker, record, error = (
            fetch_one(item)
        )

        if record is not None:

            result[ticker] = record

            previous_failures.pop(
                ticker,
                None
            )

            successful_this_run += 1

            print(
                f'OK {ticker}'
            )

        else:

            previous_failures[ticker] = (
                error
            )

            print(
                f'FAIL {ticker}: {error}'
            )

        # ----------------------------------------------------
        # Stay comfortably below SEC rate limits.
        # One request every ~0.6 sec.
        # ----------------------------------------------------

        time.sleep(0.6)

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    payload = {

        'updated_at':
            datetime.now(
                timezone.utc
            ).isoformat(),

        'source':
            'SEC EDGAR XBRL Companyfacts',

        'data':
            result,

        'errors':
            previous_failures,

        'stats': {

            'requested_stock_tickers':
                len(requested),

            'successful_records':
                len(result),

            'successful_this_run':
                successful_this_run,

            'failed_tickers':
                len(previous_failures),
        },
    }

    OUT.write_text(

        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(
                ',',
                ':'
            )
        ),

        encoding='utf-8'
    )

    print(
        f'Wrote {OUT}: '
        f'{successful_this_run} new/updated records, '
        f'{len(previous_failures)} failed/skipped records.'
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == '__main__':
    main()
