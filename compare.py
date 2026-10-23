"""시세 출처 비교 (HTS 숫자와 맞는 출처 찾기용)
환경변수: CODES='499790,030610'  DATES='2026-06-26,2026-06-23'  (같은 순서)
각 출처에서 해당 날짜 앞뒤 며칠의 시가/고가/저가/종가/거래량을 출력한다."""
import os
import re
import sys
import traceback

import pandas as pd
import requests
import FinanceDataReader as fdr

codes = [c.strip() for c in (os.environ.get('CODES') or '499790,030610').split(',') if c.strip()]
dates = [d.strip() for d in (os.environ.get('DATES') or '2026-06-26,2026-06-23').split(',') if d.strip()]
H = {'User-Agent': 'Mozilla/5.0'}


def show(title, df, d):
    print(f'  [{title}]')
    if df is None or len(df) == 0:
        print('    (데이터 없음)')
        return
    center = pd.Timestamp(d)
    part = df[(df.index >= center - pd.Timedelta(days=4)) & (df.index <= center + pd.Timedelta(days=4))]
    print(part.to_string().replace('\n', '\n    ') if len(part) else '    (그 날짜 근처 없음)')


def naver_front(code, start, end):
    url = ('https://m.stock.naver.com/front-api/external/chart/domestic/info'
           f'?symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe=day')
    r = requests.get(url, headers=H, timeout=20)
    rows = re.findall(r'\[\s*"?(\d{8})"?\s*,\s*"?([\d.]+)"?\s*,\s*"?([\d.]+)"?\s*,\s*"?([\d.]+)"?\s*,\s*"?([\d.]+)"?\s*,\s*"?(\d+)"?', r.text)
    if not rows:
        raise RuntimeError(f'파싱 실패 status={r.status_code} 앞부분={r.text[:120]!r}')
    df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume']).astype(
        {'Open': float, 'High': float, 'Low': float, 'Close': float, 'Volume': float})
    df.index = pd.to_datetime(df.pop('Date'))
    return df


def naver_api(code, start, end):
    url = f'https://api.stock.naver.com/chart/domestic/item/{code}/day?startDateTime={start}0000&endDateTime={end}0000'
    r = requests.get(url, headers=H, timeout=20)
    data = r.json()
    df = pd.DataFrame(data)
    ren = {'openPrice': 'Open', 'highPrice': 'High', 'lowPrice': 'Low', 'closePrice': 'Close', 'accumulatedTradingVolume': 'Volume'}
    df = df.rename(columns=ren)
    df.index = pd.to_datetime(df.pop('localDate'))
    return df[['Open', 'High', 'Low', 'Close', 'Volume']]


def fdr_src(code, start, end, prefix=''):
    return fdr.DataReader(prefix + code, start[:4] + '-' + start[4:6] + '-' + start[6:])


for code, d in zip(codes, dates):
    center = pd.Timestamp(d)
    start = (center - pd.Timedelta(days=10)).strftime('%Y%m%d')
    end = (center + pd.Timedelta(days=10)).strftime('%Y%m%d')
    print(f'\n===== {code}  기준 {d} =====')
    for title, fn in (('FDR 기본', lambda: fdr_src(code, start, end)),
                      ('FDR KRX:', lambda: fdr_src(code, start, end, 'KRX:')),
                      ('네이버 front-api', lambda: naver_front(code, start, end)),
                      ('네이버 api.stock', lambda: naver_api(code, start, end))):
        try:
            show(title, fn(), d)
        except Exception as e:                                  # noqa: BLE001
            print(f'  [{title}] 실패: {e!r}')
sys.exit(0)
