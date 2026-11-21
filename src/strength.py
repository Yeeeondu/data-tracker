"""업종 안에서 혼자 다르게 움직이는 종목 찾기 (sectors.yaml 의 업종 묶음 기준).

매일 (장 마감 후, --daily)
  업종 대부분과 반대로 크게 움직인 종목. 예) AI 하드웨어 대부분 하락한 날 AMD +3.4%
  - 업종 중앙값이 +0.5% 이하인데 +3% 이상 오르고 중앙값보다 3%p 이상 높음 (또는 그 반대)
  - 또는 업종 중앙값과 7%p 이상 차이
  - 미국·한국 종목은 거래일이 달라서 시장별로 따로 비교

매주 (주말, --weekly) — 업종이 약할 때만
  업종이 약하다 = 최근 4주 동안 업종 종목의 80% 이상이 52주 신고가를 못 냄
  1) 홀로 신고가: 그런 업종에서 최근 1주 안에 신고가를 냈거나 52주 고점의 97% 이상인 종목 (Dell형)
  2) 덜 빠진 종목: 업종 평균 고점 대비 하락률이 -15% 이하인데, 그보다 10%p 이상 덜 빠진 종목 (TI형)
"""
import datetime as dt
import pathlib
import statistics
import sys
import time

import requests
import yaml

from common import ROOT, UA, html_escape, notify_error_once
from news import send_long


def load_sectors():
    with open(ROOT / "sectors.yaml", encoding="utf-8") as f:
        return {g: {str(k): str(v) for k, v in (members or {}).items()} for g, members in yaml.safe_load(f).items()}


def history(ticker):
    """Yahoo Finance 일봉 2년치 [(날짜, 종가)]"""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2y&interval=1d",
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts, cl = res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []
    tz = dt.timezone(dt.timedelta(seconds=res.get("meta", {}).get("gmtoffset", 0)))
    return [(dt.datetime.fromtimestamp(t, tz).date().isoformat(), c) for t, c in zip(ts, cl) if c]


def fetch_all(tickers):
    data, fails = {}, []
    for t in tickers:
        for attempt in range(2):
            try:
                h = history(t)
                if len(h) >= 60:
                    data[t] = h
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    fails.append(f"{t}({str(e)[:40]})")
                time.sleep(2)
        time.sleep(0.3)
    if fails:
        print(f"시세 못 가져온 종목 {len(fails)}개: {', '.join(fails[:30])}", file=sys.stderr)
    return data


def market(t):
    return "KR" if t.endswith((".KS", ".KQ")) else "US"


# ───────── 매일: 업종과 반대로 움직인 종목 ─────────
def daily(sectors, data):
    hits = {}  # 티커 → (차이 크기, 문장)
    dates = {}
    for g, members in sectors.items():
        for mk in ("US", "KR"):
            rets = {}
            for t in members:
                h = data.get(t)
                if market(t) != mk or not h or len(h) < 2:
                    continue
                rets[t] = (h[-1][1] / h[-2][1] - 1) * 100
                dates[mk] = max(dates.get(mk, ""), h[-1][0])
            # 가장 최근 거래일이 아닌 종목(거래정지 등)은 빼고 비교
            rets = {t: r for t, r in rets.items() if data[t][-1][0] == dates.get(mk)}
            if len(rets) < 5:
                continue
            med = statistics.median(rets.values())
            for t, r in rets.items():
                d = r - med
                against_up = r >= 3 and med <= 0.5 and d >= 3
                against_down = r <= -3 and med >= -0.5 and d <= -3
                if against_up or against_down or abs(d) >= 7:
                    line = (f"{html_escape(members[t])} <i>({html_escape(g)})</i>: {r:+.1f}% "
                            f"｜ 업종 중앙값 {med:+.1f}% ({len(rets)}종목)")
                    if t not in hits or abs(d) > hits[t][0]:
                        hits[t] = (abs(d), line, mk)
    if not hits:
        print("오늘은 업종과 크게 다르게 움직인 종목 없음")
        return
    blocks = []
    for mk, label in (("US", "미국"), ("KR", "한국")):
        rows = sorted((v for v in hits.values() if v[2] == mk), key=lambda v: -v[0])[:12]
        if rows:
            blocks.append(f"<u>{label} ({dates.get(mk, '')})</u>\n" + "\n".join(f"- {r[1]}" for r in rows))
    send_long("⚡ <b>업종과 다르게 움직인 종목</b>\n"
              "<i>하루 등락률 기준. 같은 업종(5종목 이상) 중앙값이 +0.5% 이하인데 +3% 이상 오른 종목, "
              "중앙값이 −0.5% 이상인데 −3% 이상 내린 종목, 또는 중앙값과 7%p 이상 차이 난 종목</i>\n\n"
              + "\n\n".join(blocks))


# ───────── 매주: 약한 업종 속 강한 종목 ─────────
def stats(h):
    closes = [c for _, c in h]
    hi52 = max(closes[-252:])
    # 최근 n거래일 중 그날까지의 52주 신고가를 낸 날이 있는지
    def new_high_within(n):
        for k in range(1, n + 1):
            idx = len(closes) - k
            if idx >= 1 and closes[idx] >= max(closes[max(0, idx - 252):idx]):
                return True
        return False
    return {"close": closes[-1], "hi52": hi52, "dd": (closes[-1] / hi52 - 1) * 100,
            "nh4w": new_high_within(20), "nh1w": new_high_within(5)}


def weekly(sectors, data):
    blocks = []
    for g, members in sectors.items():
        st = {t: stats(data[t]) for t in members if t in data and len(data[t]) >= 120}  # 상장 6개월 이상 (짧으면 있는 기간 안의 고점으로)
        if len(st) < 5:
            continue
        share_nh = sum(s["nh4w"] for s in st.values()) / len(st)
        if share_nh > 0.2:
            continue  # 업종이 약하지 않음 (20% 넘게 최근 4주 안에 신고가)
        avg_dd = statistics.mean(s["dd"] for s in st.values())
        lone = [(t, s) for t, s in st.items() if s["nh1w"] or s["close"] >= s["hi52"] * 0.97]
        light = [(t, s) for t, s in st.items()
                 if avg_dd <= -15 and s["dd"] - avg_dd >= 10 and (t, s) not in lone]
        if not lone and not light:
            continue
        lines = [f"<b>{html_escape(g)}</b> — 최근 4주 신고가 {share_nh * 100:.0f}% ({round(share_nh * len(st))}/{len(st)}), "
                 f"평균 고점 대비 {avg_dd:.0f}%"]
        if lone:
            lines.append("<u>홀로 신고가·근접</u>")
            for t, s in sorted(lone, key=lambda x: -x[1]["dd"]):
                tag = "이번 주 신고가 갱신" if s["nh1w"] else f"고점 대비 {s['dd']:.1f}%"
                lines.append(f"- {html_escape(members[t])}: {tag}")
        if light:
            lines.append("<u>덜 빠진 종목</u>")
            for t, s in sorted(light, key=lambda x: -x[1]["dd"]):
                lines.append(f"- {html_escape(members[t])}: 고점 대비 {s['dd']:.0f}% (업종 평균보다 {s['dd'] - avg_dd:.0f}%p 덜 빠짐)")
        blocks.append("\n".join(lines))
    if not blocks:
        print("이번 주는 약한 업종 속 강한 종목 없음")
        return
    today = dt.date.today().strftime("%m/%d")
    send_long(f"🔎 <b>약한 업종 속 강한 종목</b> ({today})\n<i>업종 종목의 80% 이상이 최근 4주간 52주 신고가를 못 낸 업종에서, 홀로 신고가·근접(이번 주 신고가 또는 고점 대비 −3% 이내) 종목과 업종 평균이 고점 대비 −15% 이하일 때 평균보다 10%p 이상 덜 빠진 종목</i>\n\n"
              + "\n\n".join(blocks))


def main():
    sectors = load_sectors()
    tickers = list(dict.fromkeys(t for m in sectors.values() for t in m))
    data = fetch_all(tickers)
    print(f"시세 {len(data)}/{len(tickers)}종목")
    if len(data) < len(tickers) * 0.5:
        notify_error_once("업종 비교", f"시세를 절반 이상 못 가져옴 ({len(data)}/{len(tickers)})")
        sys.exit(1)
    if "--weekly" in sys.argv:
        weekly(sectors, data)
    else:
        daily(sectors, data)


if __name__ == "__main__":
    main()
