import json
import os
import re
import time
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

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


def plausible_multiple(value, lower=0.0, upper=None):
    """Return a valuation multiple only when it is positive and inside a plausible band."""
    if value is None:
        return None
    if not math.isfinite(value) or value <= lower:
        return None
    if upper is not None and value > upper:
        return None
    return value


# --- Yahoo Finance 시세·베타 (API 키 불필요) ---------------------------------
# SEC XBRL에는 가격이 없어 시가총액·PER·PSR·EV/EBITDA·베타를 만들 수 없습니다.
# Yahoo chart API에서 시세와 주간 종가를 받아 보완하되, 실패한 티커는 결측으로 두고
# 전체 수집은 계속 진행합니다.
YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart'
YAHOO_QUOTE_SUMMARY = 'https://query1.finance.yahoo.com/v10/finance/quoteSummary'
YAHOO_CRUMB_URL = 'https://query1.finance.yahoo.com/v1/test/getcrumb'
YAHOO_COOKIE_URL = 'https://fc.yahoo.com'
YAHOO_PAGE_URL = 'https://finance.yahoo.com/quote/SPY'
YAHOO_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)
YAHOO_HEADERS = {
    'User-Agent': YAHOO_UA,
    'Accept': 'application/json',
}
# 쿠키·크럼을 받을 때는 브라우저가 페이지를 요청하는 것과 같은 헤더가 필요합니다.
YAHOO_HTML_HEADERS = {
    'User-Agent': YAHOO_UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}
BENCHMARK_SYMBOL = '^GSPC'
BETA_MIN_WEEKS = 52

# Yahoo 쿠키·크럼은 한 번 받아 재사용합니다(티커마다 새 세션을 만들면 차단되기 쉽습니다).
YAHOO_AUTH_TTL = 30 * 60
CRUMB_PATTERN = re.compile(r'^[A-Za-z0-9._%~-]{4,40}$')
_YAHOO_AUTH = {'session': None, 'crumb': None, 'obtained': 0.0, 'failed_at': 0.0}


def yahoo_symbol(ticker):
    """Yahoo 표기: 클래스주 BRK.B → BRK-B."""
    return str(ticker or '').strip().upper().replace('.', '-')


def yahoo_chart(symbol, timeout=(3, 6)):
    url = f'{YAHOO_CHART}/{quote(symbol)}?range=5y&interval=1wk'
    r = requests.get(url, headers=YAHOO_HEADERS, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f'Yahoo HTTP {r.status_code}')
    payload = r.json()
    chart = payload.get('chart') or {}
    if chart.get('error'):
        raise RuntimeError(f"Yahoo error: {chart.get('error')}")
    results = chart.get('result') or []
    if not results:
        raise RuntimeError('Yahoo returned no result')
    return results[0]


def yahoo_auth(force=False):
    """Yahoo 쿠키와 크럼을 준비해 (세션, 크럼)을 돌려줍니다. 실패하면 (None, None).

    fc.yahoo.com에서 A1/A3 쿠키를 받고 getcrumb로 크럼을 받습니다. 데이터센터 IP에서는
    getcrumb가 406을 돌려주는 경우가 있어, 그때는 시세 페이지 HTML에 들어 있는 크럼을
    대신 씁니다. 어떤 실패도 전체 실행을 멈추지 않습니다.
    """
    state = _YAHOO_AUTH
    cached = state['session'] is not None and state['crumb']
    if cached and not force and (time.time() - state['obtained']) < YAHOO_AUTH_TTL:
        return state['session'], state['crumb']
    # 방금 실패했다면 티커마다 같은 시도를 되풀이하지 않습니다(1분 뒤 재시도).
    if state['failed_at'] and not force and (time.time() - state['failed_at']) < 60:
        return None, None

    crumb = None
    last_status = None
    for attempt in range(2):
        session = requests.Session()
        session.headers.update(YAHOO_HTML_HEADERS)
        try:
            # fc.yahoo.com은 404를 돌려주지만 그 응답에 A1/A3 쿠키가 담겨 옵니다.
            session.get(YAHOO_COOKIE_URL, timeout=(3, 6))
        except Exception as exc:
            last_status = f'cookie: {exc}'

        try:
            r = session.get(YAHOO_CRUMB_URL, timeout=(3, 6))
            last_status = f'getcrumb HTTP {r.status_code}'
            if r.status_code == 200 and CRUMB_PATTERN.match(r.text.strip()):
                crumb = r.text.strip()
        except Exception as exc:
            last_status = f'getcrumb: {exc}'

        if not crumb:
            try:
                r = session.get(YAHOO_PAGE_URL, timeout=(4, 12))
                last_status = f'quote page HTTP {r.status_code}'
                if r.status_code == 200:
                    match = re.search(r'"crumb":"([^"]{4,40})"', r.text)
                    if match and CRUMB_PATTERN.match(match.group(1)):
                        crumb = match.group(1)
            except Exception as exc:
                last_status = f'quote page: {exc}'

        if crumb:
            break
        time.sleep(1.5)

    if not crumb:
        state.update({
            'session': None, 'crumb': None, 'obtained': 0.0, 'failed_at': time.time(),
        })
        print(
            'WARNING: Yahoo 쿠키·크럼을 받지 못해 Forward PER·PEG·실제 시가총액은 건너뜁니다 '
            f'({last_status}).'
        )
        return None, None

    state.update({
        'session': session, 'crumb': crumb, 'obtained': time.time(), 'failed_at': 0.0,
    })
    return session, crumb


def yahoo_quote_summary(symbol, timeout=(4, 8), retry=True):
    """quoteSummary(summaryDetail·defaultKeyStatistics·price)를 받아옵니다.

    Forward PER·PEG·실제 시가총액·베타가 들어 있습니다. 401/403(크럼 만료)과
    429/5xx(과도 요청)는 크럼을 새로 받거나 짧게 쉬었다가 한 번만 다시 시도하고,
    그래도 실패하면 None을 돌려줍니다(전체 수집은 계속).
    """
    session, crumb = yahoo_auth()
    if not session:
        return None
    url = (
        f'{YAHOO_QUOTE_SUMMARY}/{quote(symbol)}'
        f'?modules=summaryDetail,defaultKeyStatistics,price&crumb={quote(crumb)}'
    )
    try:
        r = session.get(url, headers=YAHOO_HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        if retry:
            time.sleep(1.0)
            return yahoo_quote_summary(symbol, timeout, retry=False)
        print(f'WARNING {symbol}: Yahoo quoteSummary 요청 실패 — {exc}')
        return None

    if r.status_code in (401, 403) and retry:
        yahoo_auth(force=True)
        time.sleep(0.5)
        return yahoo_quote_summary(symbol, timeout, retry=False)
    if r.status_code in (429, 500, 502, 503, 504) and retry:
        time.sleep(2.0)
        return yahoo_quote_summary(symbol, timeout, retry=False)
    if r.status_code != 200:
        print(f'WARNING {symbol}: Yahoo quoteSummary HTTP {r.status_code}')
        return None

    try:
        payload = r.json()
    except ValueError:
        print(f'WARNING {symbol}: Yahoo quoteSummary 응답을 해석하지 못했습니다.')
        return None
    results = (payload.get('quoteSummary') or {}).get('result') or []
    if not results or not isinstance(results[0], dict):
        return None
    node = results[0]
    return {
        'summaryDetail': node.get('summaryDetail') or {},
        'defaultKeyStatistics': node.get('defaultKeyStatistics') or {},
        'price': node.get('price') or {},
    }


def yahoo_value(modules, key):
    """quoteSummary 값 추출. 최신 응답은 숫자, 예전 응답은 {'raw': ...} 형태입니다."""
    for module in modules:
        node = (module or {}).get(key)
        if isinstance(node, dict):
            node = node.get('raw', node.get('fmt'))
        value = num(node)
        if value is not None:
            return value
    return None


def yahoo_week_closes(node):
    """주간 종가 {날짜: 종가}. 배당 조정(adjclose)이 있으면 그것을 씁니다."""
    stamps = node.get('timestamp') or []
    indicators = node.get('indicators') or {}
    series = None
    for key in ('adjclose', 'quote'):
        block = indicators.get(key) or []
        if block and isinstance(block[0], dict):
            values = block[0].get('adjclose') if key == 'adjclose' else block[0].get('close')
            if values:
                series = values
                break
    if not stamps or not series:
        return {}
    out = {}
    for stamp, value in zip(stamps, series):
        price = num(value)
        if price is None or price <= 0:
            continue
        try:
            day = datetime.fromtimestamp(int(stamp), timezone.utc).date()
        except Exception:
            continue
        out[day] = price
    return out


def yahoo_snapshot(ticker, attempts=2):
    """(현재가, 주간 종가)를 반환. 실패하면 None (예외를 밖으로 던지지 않음)."""
    symbol = yahoo_symbol(ticker)
    last_error = None
    for attempt in range(attempts):
        try:
            node = yahoo_chart(symbol)
            meta = node.get('meta') or {}
            closes = yahoo_week_closes(node)
            price = num(meta.get('regularMarketPrice'))
            if price is None or price <= 0:
                price = closes[max(closes)] if closes else None
            return {'symbol': symbol, 'price': price, 'closes': closes}
        except Exception as exc:
            last_error = exc
            time.sleep(min(1 + attempt, 3))
    print(f'WARNING {ticker}: Yahoo 시세를 가져오지 못했습니다 — {last_error}')
    return None


def beta_vs_benchmark(stock_closes, benchmark_closes):
    """주간 수익률 5년 베타 = Cov(종목, S&P500) / Var(S&P500)."""
    if not stock_closes or not benchmark_closes:
        return None
    dates = sorted(set(stock_closes) & set(benchmark_closes))
    if len(dates) < BETA_MIN_WEEKS + 1:
        return None
    stock_returns, market_returns = [], []
    for prev, cur in zip(dates, dates[1:]):
        s0, s1 = stock_closes[prev], stock_closes[cur]
        m0, m1 = benchmark_closes[prev], benchmark_closes[cur]
        if min(s0, s1, m0, m1) <= 0:
            continue
        stock_returns.append(s1 / s0 - 1.0)
        market_returns.append(m1 / m0 - 1.0)
    if len(stock_returns) < BETA_MIN_WEEKS:
        return None
    n = len(stock_returns)
    mean_stock = sum(stock_returns) / n
    mean_market = sum(market_returns) / n
    covariance = sum(
        (s - mean_stock) * (m - mean_market)
        for s, m in zip(stock_returns, market_returns)
    ) / n
    variance = sum((m - mean_market) ** 2 for m in market_returns) / n
    if variance <= 0:
        return None
    beta = covariance / variance
    if not math.isfinite(beta) or beta <= 0 or beta > 5.0:
        return None
    return beta


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
    # SEC XBRL 실제 태그명은 InterestExpenseNonoperating입니다. 과거 코드의
    # InterestExpenseNonOperating(대문자 O) 철자 때문에 이 태그를 쓰는 기업
    # (MRK, MSFT, NVDA, KLAC 등)의 이자비용이 통째로 누락됐습니다.
    interest_arr = arr_for(facts_all, [
        ('us-gaap', [
            'InterestExpenseNonoperating',
            'InterestExpenseNonOperating',
            'InterestExpenseNonoperatingAndOperating',
            'InterestExpenseNonOperatingAndOperating',
            'InterestAndDebtExpense',
            'InterestExpense',
        ]),
        ('ifrs-full', ['FinanceCosts', 'InterestExpense']),
    ])
    # 영업이익(OperatingIncomeLoss)을 공시하지 않는 기업의 보완용 세전이익
    pretax_arr = arr_for(facts_all, [
        ('us-gaap', [
            'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
            'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments',
            'IncomeLossFromContinuingOperationsBeforeIncomeTaxes',
        ]),
        ('ifrs-full', ['ProfitLossBeforeTax']),
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

    # 영업이익을 공시하지 않는 기업(MRK 등)은 세전이익 + 이자비용(양수 가산)으로 보완합니다.
    opinc_source = 'OperatingIncomeLoss' if opinc_ttm is not None else None
    if opinc_ttm is None:
        pretax_ttm, pretax_asof = fresh_ttm(ticker, '세전이익', pretax_arr)
        if pretax_ttm is not None:
            interest_addback = abs(interest_ttm) if interest_ttm is not None else 0.0
            opinc_ttm = pretax_ttm + interest_addback
            opinc_asof = pretax_asof
            opinc_source = 'pretax_income_plus_interest'
            print(
                f'NOTE {ticker}: OperatingIncomeLoss가 없어 세전이익 '
                f'{pretax_ttm:,.0f} + 이자비용 {interest_addback:,.0f} = '
                f'{opinc_ttm:,.0f}로 영업이익을 보완했습니다.'
            )

    # 감가상각 태그가 오래됐거나 없는 기업은 감가상각비(Depreciation) + 상각비를 합산해 보완합니다.
    dep_arr = arr_for(facts_all, [
        ('us-gaap', ['Depreciation']),
        ('ifrs-full', ['DepreciationExpense']),
    ])
    dep_ttm, dep_asof = fresh_ttm(ticker, '감가상각비', dep_arr)
    if dep_ttm is not None and da_ttm is None:
        am_arr = arr_for(facts_all, [
            ('us-gaap', ['AdjustmentForAmortization', 'AmortizationOfIntangibleAssets']),
            ('ifrs-full', ['AmortisationExpense']),
        ])
        am_ttm, am_asof = fresh_ttm(ticker, '상각비', am_arr)
        components = abs(dep_ttm) + (abs(am_ttm) if am_ttm is not None else 0.0)
        if components > 0:
            print(
                f'NOTE {ticker}: 감가상각을 세부 항목으로 보완했습니다 '
                f'(감가상각비 {abs(dep_ttm):,.0f}'
                + (f' + 상각비 {abs(am_ttm):,.0f}' if am_ttm is not None else '')
                + f' = {components:,.0f}).'
            )
            da_ttm = components
            da_asof = max(d for d in (dep_asof, am_asof) if d) if (am_asof or dep_asof) else dep_asof
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
        '__operatingIncomeSource': opinc_source,
        '__asOf': as_of,
        '__sharesAsOf': shares_asof,
        '__sharesSource': shares_source,
        '__updatedAt': datetime.now(timezone.utc).isoformat(),
    }


def usable_shares(ticker, record):
    """시가총액 계산에 쓸 수 있는 발행주식수만 돌려줍니다.

    오래된 주식수(V: 2010년 값)나 1주 같은 비정상 값으로 시가총액을 만들면
    PER·PSR이 수십 배 틀어지므로, 3년 넘은 기준일과 너무 작은 값은 쓰지 않습니다.
    """
    shares = num(record.get('SharesOutstanding'))
    if shares is None or shares < 10000:
        return None
    as_of = to_date(record.get('__sharesAsOf'))
    if as_of is not None:
        age = (datetime.now(timezone.utc).date() - as_of).days
        if age > 1095:
            print(
                f'WARNING {ticker}: 발행주식수 기준일 {as_of}이 {age}일 전이라 '
                '시가총액·PER·PSR·EV/EBITDA를 계산하지 않습니다.'
            )
            return None
    return shares


def enrich_market_fields(ticker, record, benchmark_closes):
    """Yahoo 시세로 시가총액·PER·PSR·EV/EBITDA·베타·Forward PER·PEG를 보탭니다.

    시가총액은 Yahoo quoteSummary가 준 실제 달러 시총을 우선 씁니다. SEC 발행주식수가
    오래됐거나(V: 2010년 값) ADS 비율을 반영하지 못한 종목(TSM·UMC·ASX·XPEV)이
    이 한 단계로 바로잡힙니다. Yahoo 값이 없을 때만 price × SEC 주식수로 계산합니다.
    실패해도 예외를 던지지 않습니다.
    """
    snapshot = yahoo_snapshot(ticker) or {}
    summary = yahoo_quote_summary(yahoo_symbol(ticker)) or {}
    detail = summary.get('summaryDetail') or {}
    stats = summary.get('defaultKeyStatistics') or {}
    price_node = summary.get('price') or {}

    price = num(snapshot.get('price'))
    price_source = 'Yahoo Finance chart API'
    if price is None:
        price = num(price_node.get('regularMarketPrice'))
        if price is not None:
            price_source = 'Yahoo quoteSummary price.regularMarketPrice'

    # 1) 시가총액: Yahoo 실제 시총 → (없을 때만) 현재가 × SEC 발행주식수
    yahoo_cap = yahoo_value([price_node, detail], 'marketCap')
    if yahoo_cap is not None and yahoo_cap > 0:
        market_cap = yahoo_cap
        cap_source = 'Yahoo quoteSummary marketCap'
    else:
        shares = usable_shares(ticker, record)
        market_cap = price * shares if price and shares and price > 0 else None
        cap_source = 'Yahoo price x SEC SharesOutstanding' if market_cap else None
        if market_cap is not None:
            print(f'NOTE {ticker}: Yahoo 실제 시가총액이 없어 현재가 × SEC 발행주식수로 계산했습니다.')
    if market_cap is not None:
        record['MarketCapitalization'] = market_cap

    # 2) Forward PER·PEG (Yahoo 컨센서스 기준). 이상치는 비워 둡니다.
    forward_pe = plausible_multiple(
        yahoo_value([detail, stats], 'forwardPE'), 0.0, 1000.0
    )
    # PEG가 음수면 성장률이 음수라는 뜻이고, 화면의 PEG 밴드는 낮을수록 높은 점수를 주므로
    # 음수를 그대로 넣으면 평가가 뒤집힙니다. 양수만 받습니다.
    peg = plausible_multiple(
        yahoo_value([stats, detail], 'pegRatio'), 0.0, 100.0
    )
    if forward_pe is not None:
        record['ForwardPE'] = forward_pe
    if peg is not None:
        record['PEGRatio'] = peg

    net_income = num(record.get('NetIncomeTTM'))
    revenue = num(record.get('RevenueTTM'))
    ebitda = num(record.get('EBITDA'))
    debt = num(record.get('TotalDebt')) or 0.0
    cash = num(record.get('Cash')) or 0.0

    pe = plausible_multiple(
        market_cap / net_income if market_cap and net_income and net_income > 0 else None,
        0.0, 1000.0,
    )
    psr = plausible_multiple(
        market_cap / revenue if market_cap and revenue and revenue > 0 else None,
        0.0, 100.0,
    )
    enterprise_value = market_cap + debt - cash if market_cap is not None else None
    ev_ebitda = plausible_multiple(
        enterprise_value / ebitda if enterprise_value and ebitda and ebitda > 0 else None,
        0.0, 300.0,
    )

    # 3) 베타: 5년 주간 수익률 계산을 먼저 쓰고, 계산이 안 될 때만 Yahoo 베타로 보완합니다.
    beta = beta_vs_benchmark(snapshot.get('closes') or {}, benchmark_closes)
    if beta is None:
        yahoo_beta = plausible_multiple(yahoo_value([detail, stats], 'beta'), 0.0, 5.0)
        if yahoo_beta is not None:
            beta = yahoo_beta
            print(f'NOTE {ticker}: 주간 수익률로 베타를 계산하지 못해 Yahoo 베타 {beta:.2f}를 썼습니다.')

    record['PERatio'] = pe
    record['PriceToSalesRatioTTM'] = psr
    record['EVToEBITDA'] = ev_ebitda
    record['Beta'] = beta
    if price is not None:
        record['__price'] = price
        record['__priceSource'] = price_source
    if cap_source:
        record['__marketCapSource'] = cap_source

    if all(v is None for v in (market_cap, pe, psr, ev_ebitda, beta, forward_pe, peg)):
        print(f'WARNING {ticker}: Yahoo 시세는 받았지만 계산 가능한 지표가 없습니다.')
        return False
    if beta is None and benchmark_closes:
        print(f'WARNING {ticker}: 베타를 계산할 주간 수익률이 부족합니다.')
    return True


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

    # 기준지수(^GSPC)는 실행 시작에 한 번만 받아 둡니다. 실패해도 베타만 결측됩니다.
    benchmark_snapshot = yahoo_snapshot(BENCHMARK_SYMBOL)
    benchmark_closes = (benchmark_snapshot or {}).get('closes') or {}
    if benchmark_closes:
        print(f'Yahoo 기준지수 주간 종가 {len(benchmark_closes)}개를 받았습니다.')
    else:
        print('WARNING: 기준지수(^GSPC) 시세를 받지 못해 베타는 계산하지 않습니다.')

    def fetch_one(item):
        ticker, cik = item['ticker'], item['cik']
        url = f'{COMMON}/api/xbrl/companyfacts/CIK{cik}.json'
        try:
            time.sleep(0.15)  # SEC 초당 10회 제한을 넘지 않도록 간격을 둡니다.
            facts = get_json(url)
            record = build_company(ticker, cik, facts)
            return ticker, record, None
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

    # 시세 보강은 SEC 요청과 섞이지 않도록 순차로 진행합니다.
    # 티커 하나가 실패하거나 늦어져도 전체 실행이 멈추지 않도록 예외와 시간 상한을 둡니다.
    market_ok = 0
    market_failed = []
    market_deadline = time.time() + 600
    for item in requested:
        ticker = item['ticker']
        record = result.get(ticker)
        if not isinstance(record, dict):
            continue
        if time.time() > market_deadline:
            market_failed.append(ticker)
            print(f'WARNING {ticker}: 시세 보강 시간 상한을 넘어 건너뜁니다.')
            continue
        try:
            if enrich_market_fields(ticker, record, benchmark_closes):
                market_ok += 1
            else:
                market_failed.append(ticker)
        except Exception as exc:
            market_failed.append(ticker)
            print(f'WARNING {ticker}: 시세 지표 계산 실패 — {exc}')
        time.sleep(0.1)
    failed_note = ''
    if market_failed:
        failed_note = ' (' + ', '.join(sorted(market_failed)) + ')'
    print(f'Yahoo 시세 보강: {market_ok}개 적용, {len(market_failed)}개 미적용{failed_note}')

    # 결측(None) 필드는 파일에 남기지 않습니다. 시세 보강 뒤에 정리합니다.
    result = {
        ticker: {k: v for k, v in record.items() if v is not None or k.startswith('__')}
        for ticker, record in result.items()
        if isinstance(record, dict)
    }

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
            'market_metrics_applied': market_ok,
            'market_metrics_unavailable': len(market_failed),
            'market_cap_available': sum(1 for r in result.values() if num(r.get('MarketCapitalization')) is not None),
            'forward_pe_available': sum(1 for r in result.values() if num(r.get('ForwardPE')) is not None),
            'peg_ratio_available': sum(1 for r in result.values() if num(r.get('PEGRatio')) is not None),
            'beta_benchmark_weeks': len(benchmark_closes),
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f"Wrote {OUT}: {fresh_successes} fresh records, {len(result)} total records, {len(previous_failures)} failed tickers.")


if __name__ == '__main__':
    main()
