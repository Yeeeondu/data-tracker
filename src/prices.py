"""종목 코드(CUSIP) → 티커, 그리고 특정 날짜 이후 주가 상승률.

- 티커: OpenFIGI 무료 API (키 없이 분당 25회, 한 번에 10개)
- 주가: Stooq 일봉 CSV → 안 되면 Yahoo Finance
실패하면 None을 돌려주고, 메시지에서는 상승률을 빼고 보냄.
"""
import datetime as dt
import sys
import time

import requests

from common import UA


def cusip_to_ticker(cusips, cache):
    todo = [c for c in dict.fromkeys(cusips) if c and c not in cache]
    for k in range(0, len(todo), 10):
        batch = todo[k:k + 10]
        body = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]
        try:
            r = requests.post("https://api.openfigi.com/v3/mapping", json=body, timeout=30,
                              headers={"Content-Type": "application/json"})
            if r.status_code == 429:
                time.sleep(60)
                r = requests.post("https://api.openfigi.com/v3/mapping", json=body, timeout=30)
            r.raise_for_status()
            for c, res in zip(batch, r.json()):
                data = res.get("data") or []
                eq = [d for d in data if d.get("marketSector") == "Equity"] or data
                cache[c] = (eq[0].get("ticker") or "") if eq else ""
        except Exception as e:  # noqa: BLE001
            print(f"티커 변환 실패: {e}", file=sys.stderr)
            break
        time.sleep(2.5)
    return cache


def _stooq(ticker, start, end):
    sym = ticker.lower().replace("/", "-").replace(".", "-") + ".us"
    url = f"https://stooq.com/q/d/l/?s={sym}&d1={start:%Y%m%d}&d2={end:%Y%m%d}&i=d"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    rows = [line.split(",") for line in r.text.strip().splitlines()[1:] if line.count(",") >= 4]
    closes = [(row[0], float(row[4])) for row in rows if row[4] not in ("", "null")]
    return closes


def _yahoo(ticker, start, end):
    sym = ticker.replace("/", "-").replace(".", "-")
    p1 = int(dt.datetime.combine(start, dt.time()).timestamp())
    p2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time()).timestamp())
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d",
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
    return [(dt.date.fromtimestamp(t).isoformat(), c) for t, c in zip(ts, cl) if c is not None]


def change_since(ticker, since):
    """since(분기 말) 종가 대비 최근 종가 상승률(%)."""
    if not ticker:
        return None
    start, end = since - dt.timedelta(days=7), dt.date.today()
    for fn in (_stooq, _yahoo):
        try:
            closes = fn(ticker, start, end)
            base = [c for d, c in closes if d <= since.isoformat()]
            if base and closes:
                return (closes[-1][1] / base[-1] - 1) * 100
        except Exception as e:  # noqa: BLE001
            print(f"{ticker} 주가({fn.__name__}) 실패: {e}", file=sys.stderr)
    return None
