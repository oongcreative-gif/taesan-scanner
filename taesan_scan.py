"""태산기법 스캐너 - GitHub Actions용

규칙 (HTS의 LC/HC와 같은 방식)
1. 바닥 = 일중 최저가(저가). 바닥 대비 고점(고가)이 2배(+100%) 이상 오른 적이 있는 종목만 본다.
2. 고점 = 그 상승 이후 일중 최고가(고가). 고점 대비 종가가 처음 -50% 이하로 마감한 날 = '-50% 날짜'
3. -50%를 찍으면 그 한 번의 상승은 끝난 것으로 보고, 그 날부터 새 바닥을 다시 찾는다.
   (같은 종목이 다시 100% 올랐다가 -50%가 되면 새 기록으로 잡히고, 목록엔 가장 최근 것을 보여준다)
4. 고점이 2026-01-01 이후인 것만 목록에 쌓는다 (바닥은 그 이전이어도 된다).
5. 아직 -50%에 안 온 종목은 '감시'로 보여준다. 텔레그램 알림은 처음 -50%를 찍었을 때만 보낸다.
   처음 한 번은 알림 없이 기준만 저장한다.
6. 현재가(종가)가 4000원 미만인 종목은 제외한다 (시총 제한은 없음).

참고
- 시총은 오늘 기준이고, 주가는 FinanceDataReader가 주는 값(액면분할 등은 보정된 값)을 그대로 쓴다.
- 실행할 때마다 처음부터 다시 계산하므로 1월부터의 기록이 자동으로 채워진다.

환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SITE_URL, FORCE, DRY_RUN, TS_SINCE, TS_FETCH_FROM, TS_MIN_PRICE
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import FinanceDataReader as fdr

ROOT = os.environ.get('SCAN_ROOT') or os.path.dirname(os.path.abspath(__file__))
KST = ZoneInfo('Asia/Seoul')
DATA_DIR = os.path.join(ROOT, 'docs', 'data')
LIST_JSON = os.path.join(DATA_DIR, 'list.json')
STATE_JSON = os.path.join(DATA_DIR, 'state.json')
UNIVERSE_CSV = os.path.join(ROOT, 'data', 'ts_universe.csv')

RISE_X = 2.0                 # 바닥 대비 2배 = +100%
HIT_DD = -50.0               # 고점 대비 -50%
SINCE = os.environ.get('TS_SINCE') or '2026-01-01'          # 이 날짜 이후에 고점이 있는 상승만
FETCH_FROM = os.environ.get('TS_FETCH_FROM') or '2025-01-01'  # 바닥을 찾기 위해 더 일찍부터 받는다
MIN_PRICE = float(os.environ.get('TS_MIN_PRICE') or 4000)    # 현재가 4000원 이상만
ALERT_DAYS = 5               # 이 기간 안에 -50%를 찍은 종목을 알림 후보로 (알림 놓침 방지, 중복은 상태로 거름)
CHART_MAX_BARS = 420
MAX_WORKERS = 8
MAX_ERROR_RATIO = 0.2


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, allow_nan=False)


def reference_date():
    start = (datetime.now(KST) - timedelta(days=14)).strftime('%Y-%m-%d')
    return fdr.DataReader('005930', start).index[-1].date()


def send_telegram(text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print('텔레그램 설정(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)이 없어 알림은 건너뜁니다')
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True},
                          timeout=20)
        if r.status_code != 200:
            print(f'텔레그램 전송 실패: {r.status_code} {r.text[:200]}')
            return False
        return True
    except requests.RequestException as e:
        print(f'텔레그램 전송 실패: {e!r}')
        return False


def get_universe():
    try:
        lst = fdr.StockListing('KRX')
        code_col = 'Code' if 'Code' in lst.columns else 'Symbol'
        lst = lst.rename(columns={code_col: 'Code'})
        lst = lst[lst['Market'].astype(str).str.startswith('KOS')]
        lst = lst[~lst['Name'].str.contains(r'스팩|제\d+호|ETN|ETF', regex=True, na=False)]
        lst = lst[~lst['Name'].str.contains(r'우$|우B$|우\(전환\)$', regex=True, na=False)]
        if 'Dept' in lst.columns:
            lst = lst[~lst['Dept'].astype(str).str.contains('관리|SPAC', na=False)]
        print(f"KRX 목록 {len(lst)}종목, 시총 값 있는 종목 {int(lst['Marcap'].notna().sum())}개")
        lst = lst.assign(Marcap=lst['Marcap'].fillna(0)).sort_values('Marcap', ascending=False)
        if len(lst) == 0:
            raise RuntimeError('종목 목록이 비어 있습니다')
        uni = lst[['Code', 'Name', 'Market', 'Marcap']].reset_index(drop=True)
        uni['Code'] = uni['Code'].astype(str).str.zfill(6)
        os.makedirs(os.path.dirname(UNIVERSE_CSV), exist_ok=True)
        uni.to_csv(UNIVERSE_CSV, index=False)
        return uni
    except Exception as e:                                  # noqa: BLE001
        print(f'KRX 종목 목록 조회 실패: {e!r}')
        if os.path.exists(UNIVERSE_CSV):
            cached = pd.read_csv(UNIVERSE_CSV, dtype={'Code': str})
            if len(cached):
                print('-> 저장해 둔 data/ts_universe.csv 를 대신 사용합니다')
                return cached
        raise


def episodes(lo, hi_, c):
    """저가·고가·종가 배열 -> 상승/하락 한 사이클 목록.
    각 항목: 바닥(li, 저가)·고점(hi, 고가) 위치와 가격, -50% 도달 위치(hit, 아직이면 None)"""
    n = len(c)
    out = []
    if n == 0:
        return out
    low, li, high, hi, q = lo[0], 0, hi_[0], 0, False
    for i in range(1, n):
        if not q:
            if lo[i] < low:
                low, li, high, hi = lo[i], i, hi_[i], i
            else:
                if hi_[i] > high:
                    high, hi = hi_[i], i
                if high >= RISE_X * low:
                    q = True
        else:
            if hi_[i] > high:
                high, hi = hi_[i], i
            if (c[i] / high - 1) * 100 <= HIT_DD:
                out.append({'li': li, 'hi': hi, 'low': low, 'high': high, 'hit': i})
                low, li, high, hi, q = lo[i], i, hi_[i], i, False
    if q:
        out.append({'li': li, 'hi': hi, 'low': low, 'high': high, 'hit': None})
    return out


def analyze(df, since):
    """가장 최근(고점이 since 이후인) 사이클의 결과 dict. 없으면 None"""
    c = df['Close'].to_numpy(dtype=float)
    eps = [e for e in episodes(df['Low'].to_numpy(dtype=float), df['High'].to_numpy(dtype=float), c) if df.index[e['hi']] >= pd.Timestamp(since)]
    if not eps:
        return None, None
    e = eps[-1]
    close = float(c[-1])
    prev = float(c[-2]) if len(c) > 1 else close
    dd_now = (close / e['high'] - 1) * 100
    d = lambda k: df.index[k].date().isoformat()          # noqa: E731
    res = {
        'status': 'hit' if e['hit'] is not None else 'watch',
        'bottom_date': d(e['li']), 'bottom_price': round(e['low'], 2),
        'peak_date': d(e['hi']), 'peak_price': round(e['high'], 2),
        'rise_pct': round((e['high'] / e['low'] - 1) * 100, 1),
        'hit_date': d(e['hit']) if e['hit'] is not None else None,
        'hit_close': round(float(c[e['hit']]), 2) if e['hit'] is not None else None,
        'half_price': round(e['high'] * (1 + HIT_DD / 100), 2),
        'close': close, 'change_pct': round((close / prev - 1) * 100, 2) if prev else 0.0,
        'dd_now': round(dd_now, 1),
        'prev_hits': [d(x['hit']) for x in eps[:-1] if x['hit'] is not None],
    }
    return res, e


def make_chart(df, e, res):
    start = max(0, e['li'] - 15, len(df) - CHART_MAX_BARS)
    tail = df.iloc[start:]

    def col(name, nd=2):
        return [None if pd.isna(x) else round(float(x), nd) for x in tail[name]]

    return {'t': [x.date().isoformat() for x in tail.index],
            'o': col('Open'), 'h': col('High'), 'l': col('Low'), 'c': col('Close'),
            'bottom_date': res['bottom_date'], 'peak_date': res['peak_date'], 'hit_date': res['hit_date'],
            'peak_price': res['peak_price'], 'half_price': res['half_price']}


def scan_one(code, start, asof):
    last_err = None
    for _ in range(3):
        try:
            raw = fdr.DataReader(code, start)
            if raw is None or raw.empty:
                return 'none', None
            if (asof - raw.index[-1].date()).days > 7:        # 오래 거래정지인 종목
                return 'none', None
            df = raw.dropna(subset=['Open', 'High', 'Low', 'Close']).copy()
            df = df[(df['Close'] > 0) & (df['Volume'] > 0)]
            if len(df) < 30:
                return 'none', None
            if float(df['Close'].iloc[-1]) < MIN_PRICE:           # 현재가 4000원 미만
                return 'none', None
            res, e = analyze(df, SINCE)
            if res is None:
                return 'none', None
            return 'ok', (res, make_chart(df, e, res))
        except Exception as ex:                             # noqa: BLE001
            last_err = repr(ex)
            time.sleep(0.7)
    return 'error', last_err


def key_of(kind, it):
    return f"{kind}:{it['code']}@{it['peak_date']}"


def alert_candidates(items, asof):
    """(키, 항목): 최근(ALERT_DAYS일 안에) 처음 -50%를 찍은 종목. 이미 알린 종목은 상태로 거른다"""
    limit = (datetime.fromisoformat(asof) - timedelta(days=ALERT_DAYS)).date().isoformat()
    return [(key_of('hit', it), it) for it in items if it['status'] == 'hit' and it['hit_date'] >= limit]


def build_messages(fresh, asof):
    site = os.environ.get('SITE_URL', '')
    msgs = []
    for k in range(0, len(fresh), 20):
        part = fresh[k:k + 20]
        lines = [f'태산기법 -50% 도달 {len(fresh)}종목 ({asof})', '']
        for _, it in part:
            lines.append(f"{it['name']} ({it['code']}) {it['hit_date']} 도달")
            lines.append(f"  고점 {it['peak_price']:,.0f}원({it['peak_date']}) 대비 {it['dd_now']:+.1f}% / "
                         f"현재가 {it['close']:,.0f}원 / 시총 {it['marcap'] / 1e12:.1f}조 / 바닥 대비 +{it['rise_pct']:,.0f}%")
        if site and k + 20 >= len(fresh):
            lines += ['', site.rstrip('/') + '/']
        msgs.append('\n'.join(lines))
    return msgs


def main():
    now = datetime.now(KST)
    force = os.environ.get('FORCE') == '1'
    dry = os.environ.get('DRY_RUN') == '1'
    asof = reference_date()
    if not force:
        if asof != now.date():
            print(f'오늘({now.date()})은 휴장이거나 아직 데이터가 없어요 (최근 거래일 {asof}). 종료합니다.')
            return 0
        if now.hour < 16:
            print('장 마감 후 데이터가 확정되기 전(16시 이전)이라 종료합니다.')
            return 0

    uni = get_universe()
    print(f'기준일 {asof} / 대상 {len(uni)}종목 (현재가 {MIN_PRICE:,.0f}원 이상만) / 고점 {SINCE} 이후, 데이터 {FETCH_FROM}부터')
    items, charts, errors, first_error = [], {}, 0, None
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(scan_one, r.Code, FETCH_FROM, asof): r for r in uni.itertuples()}
        for n, f in enumerate(as_completed(futs), start=1):
            r = futs[f]
            status, payload = f.result()
            if status == 'ok':
                res, chart = payload
                items.append({'code': r.Code, 'name': r.Name, 'market': r.Market, 'marcap': int(r.Marcap), **res})
                charts[r.Code] = {**chart, 'code': r.Code, 'name': r.Name}
            elif status == 'error':
                errors += 1
                first_error = first_error or payload
            if n % 500 == 0:
                print(f'  {n}/{len(futs)} 확인 중...')
    items.sort(key=lambda x: (x['hit_date'] or '', x['marcap']), reverse=True)
    hits = [i for i in items if i['status'] == 'hit']
    print(f"100% 상승 종목 {len(items)}개 (-50% 도달 {len(hits)}, 감시 {len(items) - len(hits)}), 조회 실패 {errors}종목")
    if len(uni) and errors / len(uni) > MAX_ERROR_RATIO:
        print(f'조회 실패가 너무 많아 결과를 저장하지 않습니다. 첫 에러: {first_error}')
        return 1

    for code, ch in charts.items():
        save_json(os.path.join(DATA_DIR, 'charts', f'{code}.json'), ch)
    save_json(LIST_JSON, {
        'updated_at': now.isoformat(timespec='seconds'), 'asof': asof.isoformat(), 'since': SINCE,
        'params': {'rise_pct': (RISE_X - 1) * 100, 'hit_dd': HIT_DD, 'min_price': MIN_PRICE},
        'items': items,
    }, indent=1)
    if dry:
        print('DRY_RUN: 상태 저장과 알림은 건너뜁니다')
        return 0

    cands = alert_candidates(items, asof.isoformat())
    state = load_json(STATE_JSON, None)
    have_tg = bool(os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'))
    if state is None:
        print(f'처음 실행이라 알림 없이 최근 -50% 도달 {len(cands)}종목을 기준으로만 저장합니다')
        save_json(STATE_JSON, {'asof': asof.isoformat(), 'keys': sorted(k for k, _ in cands)})
        return 0
    prev = set(state.get('keys', []))
    fresh = [c for c in cands if c[0] not in prev]
    exit_code, ok = 0, True
    if fresh:
        fresh.sort(key=lambda c: c[1]['dd_now'])
        ok = all([send_telegram(m) for m in build_messages(fresh, asof.isoformat())])
        if ok:
            print(f'텔레그램 알림 {len(fresh)}종목 전송')
        elif have_tg:
            exit_code = 1
    if ok or not have_tg:
        save_json(STATE_JSON, {'asof': asof.isoformat(), 'keys': sorted(k for k, _ in cands)})
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
