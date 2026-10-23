"""시세 출처 비교 2 - 통합(정규장+넥스트레이드) 시세를 주는 주소가 있는지 시험한다
환경변수: CODES, DATES (같은 순서). 결과를 그대로 출력만 한다."""
import json
import os
import sys

import requests

codes = [c.strip() for c in (os.environ.get('CODES') or '499790').split(',') if c.strip()]
dates = [d.strip() for d in (os.environ.get('DATES') or '2026-06-26').split(',') if d.strip()]
H = {'User-Agent': 'Mozilla/5.0'}


def get(url, **kw):
    try:
        r = requests.get(url, headers=H, timeout=20, **kw)
        return r.status_code, r.text
    except Exception as e:                                     # noqa: BLE001
        return 0, repr(e)


def show(title, url):
    code, text = get(url)
    print(f'\n--- {title}\n    {url}\n    status={code} 길이={len(text)}')
    print('    ' + text[:700].replace('\n', ' '))


for code, d in zip(codes, dates):
    ymd = d.replace('-', '')
    s = f'{ymd}0000'
    print(f'\n===== {code}  기준 {d} =====')
    base = f'https://api.stock.naver.com/chart/domestic/item/{code}/day?startDateTime={s}&endDateTime={s}'
    show('기본', base)
    for t in ('KRX', 'NXT', 'SOR', 'TOTAL', 'UNION'):
        show(f'stockExchangeType={t}', base + f'&stockExchangeType={t}')
    show('exchangeType=NXT', base + '&exchangeType=NXT')
    for sym in (code, code + '.NX', code + '_NX', 'NXT:' + code):
        show(f'front-api symbol={sym}',
             f'https://m.stock.naver.com/front-api/external/chart/domestic/info?symbol={sym}&requestType=1&startTime={ymd}&endTime={ymd}&timeframe=day')
    show('실시간(polling) 키 확인', f'https://polling.finance.naver.com/api/realtime/domestic/stock/{code}')
    show('basic', f'https://m.stock.naver.com/api/stock/{code}/basic')
    show('integration', f'https://m.stock.naver.com/api/stock/{code}/integration')
sys.exit(0)
