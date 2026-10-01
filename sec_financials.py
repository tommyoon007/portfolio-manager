import json, os, time, math
from datetime import datetime, timezone
from pathlib import Path
import requests

OUT = Path('sec_financials.json')
TICKERS_FILE = Path('sec_tickers.json')
UA = os.environ.get('SEC_USER_AGENT', 'Portfolio Manager GitHub Action https://github.com/')
HEADERS = {'User-Agent': UA, 'Accept-Encoding': 'gzip, deflate'}

COMMON = 'https://data.sec.gov'


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def latest_units(facts, tags, unit='USD'):
    for tag in tags:
        obj = facts.get('us-gaap', {}).get(tag)
        if not obj:
            continue
        units = obj.get('units', {})
        arr = units.get(unit) or units.get('shares') or []
        if arr:
            return tag, arr
    return None, []


def duration_facts(arr):
    out=[]
    for x in arr:
        if x.get('start') and x.get('end') and num(x.get('val')) is not None:
            y=dict(x); y['val']=num(x['val']); out.append(y)
    return sorted(out, key=lambda x:(x.get('end',''),x.get('filed','')))


def instant_facts(arr):
    out=[]
    for x in arr:
        if not x.get('start') and num(x.get('val')) is not None:
            y=dict(x); y['val']=num(x['val']); out.append(y)
    return sorted(out, key=lambda x:(x.get('end',''),x.get('filed','')))


def annual_or_quarterly(arr, n=8):
    rows=duration_facts(arr)
    # Prefer 10-Q/10-K, exclude duplicate amended records by end date+fp keeping latest filed.
    ded={}
    for r in rows:
        key=(r.get('end'), r.get('fp'), r.get('form'))
        ded[key]=r
    rows=list(ded.values())
    return rows[-n:]


def quarter_values(arr):
    rows=duration_facts(arr)
    # Use facts whose durations are roughly quarterly (<=120 days), and de-duplicate by end date.
    q=[]
    for r in rows:
        try:
            days=(datetime.fromisoformat(r['end'])-datetime.fromisoformat(r['start'])).days
        except Exception:
            continue
        if 70 <= days <= 120:
            q.append(r)
    ded={}
    for r in q:
        ded[r['end']]=r
    return sorted(ded.values(), key=lambda x:x['end'])


def ttm_from_quarters(arr):
    q=quarter_values(arr)
    if len(q) >= 4:
        return sum(x['val'] for x in q[-4:])
    return None


def latest_q_growth(arr):
    q=quarter_values(arr)
    if len(q) < 5: return None
    a=q[-1]['val']; b=q[-5]['val']
    if b == 0: return None
    return (a/b-1)*100


def fact_latest(arr):
    rows=instant_facts(arr)
    return rows[-1]['val'] if rows else None


def fact_duration_latest(arr):
    rows=duration_facts(arr)
    return rows[-1]['val'] if rows else None


def first_tag(facts, tags, mode='duration'):
    for tag in tags:
        arr=facts.get('us-gaap',{}).get(tag,{}).get('units',{}).get('USD',[])
        if not arr: continue
        v = fact_duration_latest(arr) if mode=='duration' else fact_latest(arr)
        if v is not None: return v
    return None


def build_company(ticker, cik, facts):
    gaap=facts.get('facts',{})
    # companyfacts nests taxonomy under facts
    facts_all=gaap
    revenue_tags=['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet']
    ni_tags=['NetIncomeLoss','ProfitLoss','NetIncomeLossAvailableToCommonStockholdersBasic']
    ocf_tags=['NetCashProvidedByUsedInOperatingActivities','CashAndCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect']
    capex_tags=['PaymentsToAcquirePropertyPlantAndEquipment','PaymentsToAcquireProductiveAssets']
    assets_tags=['Assets']; liab_tags=['Liabilities']; equity_tags=['StockholdersEquity','StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest']
    cash_tags=['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents']
    debt_tags=['LongTermDebtAndFinanceLeaseObligationsCurrent','LongTermDebtCurrent','LongTermDebtNoncurrent','LongTermDebtAndFinanceLeaseObligationsNoncurrent']
    current_assets=['AssetsCurrent']; current_liab=['LiabilitiesCurrent']
    opinc_tags=['OperatingIncomeLoss']
    interest_tags=['InterestExpenseNonOperating','InterestExpenseNonOperatingAndOperating']
    da_tags=['DepreciationDepletionAndAmortization','DepreciationDepletionAndAmortizationPropertyPlantAndEquipment']
    shares_tags=['EntityCommonStockSharesOutstanding']

    revenue_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in revenue_tags if facts_all.get('us-gaap',{}).get(t)), [])
    ni_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in ni_tags if facts_all.get('us-gaap',{}).get(t)), [])
    ocf_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in ocf_tags if facts_all.get('us-gaap',{}).get(t)), [])
    capex_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in capex_tags if facts_all.get('us-gaap',{}).get(t)), [])

    revenue_ttm=ttm_from_quarters(revenue_arr) or fact_duration_latest(revenue_arr)
    ni_ttm=ttm_from_quarters(ni_arr) or fact_duration_latest(ni_arr)
    ocf_ttm=ttm_from_quarters(ocf_arr) or fact_duration_latest(ocf_arr)
    capex_ttm=ttm_from_quarters(capex_arr) or fact_duration_latest(capex_arr)
    fcf=(ocf_ttm - abs(capex_ttm)) if ocf_ttm is not None and capex_ttm is not None else None

    opinc_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in opinc_tags if facts_all.get('us-gaap',{}).get(t)), [])
    interest_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in interest_tags if facts_all.get('us-gaap',{}).get(t)), [])
    da_arr=next((facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]) for t in da_tags if facts_all.get('us-gaap',{}).get(t)), [])

    opinc_ttm=ttm_from_quarters(opinc_arr) or fact_duration_latest(opinc_arr)
    interest_ttm=ttm_from_quarters(interest_arr) or fact_duration_latest(interest_arr)
    da_ttm=ttm_from_quarters(da_arr) or fact_duration_latest(da_arr)
    ebitda=(opinc_ttm+abs(da_ttm)) if opinc_ttm is not None and da_ttm is not None else None

    assets=fact_latest(facts_all.get('us-gaap',{}).get('Assets',{}).get('units',{}).get('USD',[]))
    liabilities=fact_latest(facts_all.get('us-gaap',{}).get('Liabilities',{}).get('units',{}).get('USD',[]))
    equity=None
    for t in equity_tags:
        equity=fact_latest(facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]))
        if equity is not None: break
    cash=None
    for t in cash_tags:
        cash=fact_latest(facts_all.get('us-gaap',{}).get(t,{}).get('units','').get('USD',[])) if isinstance(facts_all.get('us-gaap',{}).get(t,{}).get('units',{}),dict) else None
        if cash is not None: break
    ca=fact_latest(facts_all.get('us-gaap',{}).get('AssetsCurrent',{}).get('units',{}).get('USD',[]))
    cl=fact_latest(facts_all.get('us-gaap',{}).get('LiabilitiesCurrent',{}).get('units',{}).get('USD',[]))

    debt=0
    for t in debt_tags:
        v=fact_latest(facts_all.get('us-gaap',{}).get(t,{}).get('units',{}).get('USD',[]))
        if v is not None: debt += max(0,v)

    shares=None
    shobj=facts_all.get('dei',{}).get('EntityCommonStockSharesOutstanding',{}).get('units',{}).get('shares',[])
    if shobj: shares=instant_facts(shobj)[-1]['val']
    if shares is None:
        shares=fact_latest(facts_all.get('us-gaap',{}).get('CommonStocksIncludingAdditionalPaidInCapital',{}).get('units',{}).get('USD',[]))

    market_cap=None
    # Market cap is filled by app using current price when SEC shares are available.
    out={
      'RevenueTTM': revenue_ttm,
      'QuarterlyRevenueGrowthYOY': latest_q_growth(revenue_arr),
      'QuarterlyEarningsGrowthYOY': latest_q_growth(ni_arr),
      'OperatingIncomeTTM': opinc_ttm,
      'NetIncomeTTM': ni_ttm,
      'OperatingMarginTTM': (opinc_ttm/revenue_ttm*100) if opinc_ttm is not None and revenue_ttm else None,
      'ReturnOnEquityTTM': (ni_ttm/equity*100) if ni_ttm is not None and equity not in (None,0) else None,
      'DebtToEquity': (debt/equity*100) if equity not in (None,0) else None,
      'CurrentRatio': (ca/cl) if ca is not None and cl not in (None,0) else None,
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
      'MarketCapitalization': market_cap,
      'TotalDebt': debt,
      'Cash': cash,
      '__source':'SEC XBRL companyfacts',
      '__cik':cik,
      '__updatedAt':datetime.now(timezone.utc).isoformat()
    }
    return {k:v for k,v in out.items() if v is not None or k.startswith('__')}


def main():
    tickers=json.loads(TICKERS_FILE.read_text(encoding='utf-8'))
    mapping=requests.get('https://www.sec.gov/files/company_tickers.json',headers=HEADERS,timeout=30).json()
    by_ticker={str(v['ticker']).upper():str(v['cik_str']).zfill(10) for v in mapping.values()}
    result={}
    for ticker in tickers:
        t=ticker.upper().strip()
        cik=by_ticker.get(t)
        if not cik:
            print('CIK not found',t); continue
        try:
            r=requests.get(f'{COMMON}/api/xbrl/companyfacts/CIK{cik}.json',headers=HEADERS,timeout=45)
            r.raise_for_status()
            facts=r.json()
            result[t]=build_company(t,cik,facts)
            print('OK',t)
            time.sleep(0.2)
        except Exception as e:
            print('FAIL',t,e)
    payload={'updated_at':datetime.now(timezone.utc).isoformat(),'source':'SEC EDGAR XBRL Companyfacts','data':result}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8')

if __name__=='__main__': main()
