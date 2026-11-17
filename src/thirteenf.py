"""분기별 13F — 새 보고서가 SEC에 올라오면 보유 종목별 평가액과 직전 분기 대비 주식 수 변화를 전송."""
import datetime as dt
import json
import re
import os
import sys
import time
import xml.etree.ElementTree as ET

import requests

from common import html_escape, load_config, load_state, notify_error_once, save_state
from news import ask_claude_json, llm_available, send_long
from prices import change_since, cusip_to_ticker


def _ua():
    # SEC는 연락처가 들어간 User-Agent를 요구함 → 저장소 Secrets의 SEC_USER_AGENT 사용 (예: "Hong Gildong you@mail.com")
    return os.environ.get("SEC_USER_AGENT") or "personal-data-tracker github-actions"


def sec_get(url, as_json=True):
    for attempt in range(3):
        r = requests.get(url, headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"}, timeout=60)
        time.sleep(0.25)  # SEC 초당 10회 제한
        if r.ok:
            return r.json() if as_json else r.text
        if r.status_code in (403, 429, 503):
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
    r.raise_for_status()


def list_13f(cik):
    j = sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
    f = j["filings"]["recent"]
    out = []
    for i, form in enumerate(f["form"]):
        if form == "13F-HR":
            out.append({"acc": f["accessionNumber"][i], "filed": f["filingDate"][i],
                        "period": f["reportDate"][i]})
    return out  # 최신순


def holdings(cik, acc):
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    idx = sec_get(f"{base}/index.json")
    xmls = [i for i in idx["directory"]["item"]
            if i["name"].lower().endswith(".xml") and "primary_doc" not in i["name"].lower()]
    if not xmls:
        raise RuntimeError(f"보유 종목 표를 찾지 못함: {base}")
    xmls.sort(key=lambda i: int(i.get("size") or 0), reverse=True)
    root = ET.fromstring(sec_get(f"{base}/{xmls[0]['name']}", as_json=False).encode())

    def txt(el, tag):
        for c in el.iter():
            if c.tag.split("}")[-1] == tag:
                return (c.text or "").strip()
        return ""

    rows = {}
    for it in root.iter():
        if it.tag.split("}")[-1] != "infoTable":
            continue
        cusip, pc = txt(it, "cusip"), txt(it, "putCall").upper()
        key = f"{cusip}|{pc}"
        r = rows.setdefault(key, {"name": txt(it, "nameOfIssuer").title(), "cusip": cusip,
                                  "putcall": pc, "shares": 0.0, "value": 0.0})
        r["shares"] += float(txt(it, "sshPrnamt") or 0)
        r["value"] += float(txt(it, "value") or 0)
    # 단위 보정: 2023년부터 달러 단위가 원칙이지만, 아직 천 달러로 내는 곳이 있음 → 주당 가격으로 판별
    ratios = sorted(r["value"] / r["shares"] for r in rows.values() if r["shares"] > 0 and r["value"] > 0)
    if ratios and ratios[len(ratios) // 2] < 1.0:
        for r in rows.values():
            r["value"] *= 1000
    return rows


def money(v):
    if v >= 1e9:
        return "$" + f"{v / 1e9:.2f}".rstrip("0").rstrip(".") + "B"
    if v >= 1e6:
        return f"${v / 1e6:,.0f}M"
    return f"${v / 1e3:,.0f}K"


# 알아보기 쉬운 짧은 영문 이름 (자주 나오는 종목만; 나머지는 회사 형태 꼬리표만 떼어 냄)
ALIAS = {
    "space exploration": "SpaceX", "nvidia": "NVIDIA", "arm": "ARM", "taiwan semiconductor": "TSMC",
    "advanced micro": "AMD", "meta platforms": "Meta", "bank amer": "Bank of America",
    "bank of america": "Bank of America", "occidental pete": "Occidental", "lilly eli": "Eli Lilly",
    "eli lilly": "Eli Lilly", "coca cola": "Coca-Cola", "moodys": "Moody's", "unitedhealth": "UnitedHealth",
    "jpmorgan": "JPMorgan", "spdr s&p 500": "SPY", "spdr s p 500": "SPY", "invesco qqq": "QQQ",
    "ishares russell 2000": "IWM", "international business machines": "IBM", "american express": "AmEx",
    "d r horton": "D.R. Horton", "kraft heinz": "Kraft Heinz", "sirius xm": "Sirius XM", "asml": "ASML",
    "ge vernova": "GE Vernova", "iren": "IREN", "amazon com": "Amazon", "paypal": "PayPal",
}
TAIL = re.compile(r"\b(inc|corp|corporation|co|company|companies|ltd|limited|plc|n\.?v|s\.?a|ag|se|lp|l\.?p|"
                  r"holdings?|hldgs?|group|grp|new|del|adr|ads|sponsored|spon|ord|shs|com|common|stock|"
                  r"cl [a-c]|class [a-c]|ser [a-c]|cap stk|etf|tr|trust|vt|sh|lp units?|depositary|receipts?)\b\.?", re.I)


def short_name(name):
    low = re.sub(r"[^a-z0-9& ]+", " ", name.lower())
    low = re.sub(r"\s+", " ", low).strip()
    for k in sorted(ALIAS, key=len, reverse=True):
        if low == k or low.startswith(k + " "):
            return ALIAS[k]
    n = TAIL.sub(" ", name.replace(",", " "))
    n = re.sub(r"\s+", " ", n).strip(" .-&/")
    n = " ".join(w.capitalize() for w in n.split()) if n.isupper() else n
    return n or name


def share_class(name):
    m = re.search(r"\b(?:cl|class)\s*([a-c])\b", name, re.I)
    return m.group(1).upper() if m else ""


def label(r, dup=(), ind=None):
    n = short_name(r["name"])
    if n in dup and share_class(r["name"]):
        n += f" {share_class(r['name'])}"
    tags = [t for t in ((ind or {}).get(r["cusip"], ""), {"CALL": "Call", "PUT": "Put"}.get(r["putcall"], "")) if t]
    return n + (f" ({', '.join(tags)})" if tags else "")


def industries(rows, cache):
    """종목별 산업을 짧은 한국어로 (예: 반도체, 클라우드, 우주항공). API 키가 없으면 빈 값.
    한 번 분류한 종목은 state에 저장해 두고 다시 묻지 않음."""
    todo = {r["cusip"]: r["name"] for r in rows if r["cusip"] not in cache}
    if todo and llm_available():
        prompt = ("다음은 미국 13F에 신고된 종목(CUSIP: 회사명)이다. 각 회사의 주력 산업을 한국어 2~8글자로 짧게 적어라 "
                  "(예: 반도체, 클라우드, 우주항공, 전자상거래, 보험, 은행, 석유가스, 제약, 전력, ETF). "
                  '출력: {"CUSIP": "산업", ...}\n' + json.dumps(todo, ensure_ascii=False))
        try:
            got = ask_claude_json(prompt, max_tokens=2000)
            cache.update({k: str(v)[:12] for k, v in got.items() if k in todo})
        except Exception as e:  # noqa: BLE001
            print(f"산업 분류 실패: {e}", file=sys.stderr)
    return cache


def quarter(period):
    y, m = int(period[:4]), int(period[5:7])
    return f"{(m - 1) // 3 + 1}Q{y % 100:02d}"


def pct(x):
    return f"{x:+.0f}%".replace("+0%", "0%").replace("-0%", "0%")


def money_delta(d, ref):
    """평가액 증감: 기준 금액 단위(B/M)에 맞춰 +0.23B / -45M"""
    sign = "+" if d >= 0 else "-"
    d = abs(d)
    if ref >= 1e9:
        return f"{sign}{d / 1e9:.2f}B"
    if ref >= 1e6 or d >= 1e6:
        return f"{sign}{d / 1e6:,.0f}M"
    return f"{sign}{d / 1e3:,.0f}K"


def shares_fmt(n):
    sign = "+" if n >= 0 else "-"
    n = abs(n)
    if n >= 1e6:
        return f"{sign}{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{sign}{n / 1e3:.1f}K"
    return f"{sign}{n:,.0f}"


def after(chg):
    """｜ 분기 말 이후 고점 상승률, 현재 상승률"""
    return "" if not chg else f" ｜ {chg[0]:+.1f}%, {chg[1]:+.1f}%"


def flow_summary(cur, prev):
    """분기 중 사고판 금액 (13F엔 거래 가격이 없어 분기 말 주가로 환산).
    매수 = 신규 종목 평가액 + 늘린 주식 수 × 이번 분기 말 주가
    매도 = 전량 매도 종목의 직전 평가액 + 줄인 주식 수 × 이번 분기 말 주가"""
    buy = sell = 0.0
    for k, r in cur.items():
        p = prev.get(k)
        if p is None:
            buy += r["value"]
        elif r["shares"] and p["shares"]:
            px = r["value"] / r["shares"]
            d = r["shares"] - p["shares"]
            if d > 0:
                buy += d * px
            else:
                sell += -d * px
    for k, p in prev.items():
        if k not in cur:
            sell += p["value"]
    return buy, sell


def build_message(fund, filing, cur, prev, cfg, ind=None, moves=None):
    """주식 수 기준 신규·확대·축소·유지·전량 매도로 나눠서:
    - Natera (유전자진단): 평가액 $1.31B(+0.23B, +21%), 비중 29.2%(+12.6%p), 주식수 +120K(+11%) ｜ +8.4%
    ｜ 뒤는 분기 말 이후 지금까지의 주가 상승률."""
    n = cfg.get("top_n", 15)
    moves = moves or {}
    tot = sum(r["value"] for r in cur.values()) or 1
    ptot = sum(r["value"] for r in prev.values()) or 1
    names = [short_name(r["name"]) for r in list(cur.values()) + list(prev.values())]
    dup = {x for x in names if names.count(x) > 1}
    groups = {"신규": [], "확대": [], "축소": [], "유지": [], "전량 매도": []}
    for key, r in cur.items():
        p = prev.get(key)
        if not prev:
            groups["유지"].append((r, None))
        elif p is None:
            groups["신규"].append((r, None))
        else:
            ch = (r["shares"] / p["shares"] - 1) if p["shares"] else 0
            groups["확대" if ch > 0.005 else "축소" if ch < -0.005 else "유지"].append((r, p))
    for key, p in prev.items():
        if key not in cur:
            groups["전량 매도"].append((None, p))

    def line(r, p):
        nm = html_escape(label(r or p, dup, ind))
        chg = after(moves.get((r or p)["cusip"]))
        if r is None:  # 전량 매도
            return f"- {nm}: 직전 평가액 {money(p['value'])}, 직전 비중 {p['value'] / ptot * 100:.1f}%{chg}"
        w = r["value"] / tot * 100
        if p is None:  # 신규 (또는 비교할 직전 보고서 없음)
            return f"- {nm}: 평가액 {money(r['value'])}, 비중 {w:.1f}%{chg}"
        dv = r["value"] - p["value"]
        dvp = f", {pct(dv / p['value'] * 100)}" if p["value"] else ""
        pw = p["value"] / ptot * 100
        ds = r["shares"] - p["shares"]
        dsp = pct(ds / p["shares"] * 100) if p["shares"] else "-"
        same = p["shares"] and abs(ds / p["shares"]) <= 0.005  # 유지: 주식수 변화 생략
        shares = "" if same else f", 주식수 {shares_fmt(ds)}({dsp})"
        return (f"- {nm}: 평가액 {money(r['value'])}({money_delta(dv, r['value'])}{dvp}), "
                f"비중 {w:.1f}%({w - pw:+.1f}%p){shares}{chg}")

    lines = [f"<b>{html_escape(fund['name'])} — {quarter(filing['period'])} 13F 공시</b>"]
    if prev:
        buy, sell = flow_summary(cur, prev)
        lines.append(f"총 평가액 {money(tot)}({money_delta(tot - ptot, tot)}, {pct((tot / ptot - 1) * 100)})")
        lines.append(f"매수 {money(buy)} · 매도 {money(sell)} · 순{'매수' if buy >= sell else '매도'} {money(abs(buy - sell))} "
                     f"<i>(분기 말 주가로 환산)</i>")
        lines.append(f"종목 수 {len(cur)}개 (직전 {len(prev)}개)")
    lines.append(f"<i>｜ 뒤: 분기 말({filing['period']}) 이후 고점 상승률, 현재 상승률</i>")
    for g, items in groups.items():
        if not items:
            continue
        items.sort(key=lambda x: -((x[0] or x[1])["value"]))
        lines += ["", f"<u>{g}</u> ({len(items)})"]
        lines += [line(r, p) for r, p in items[:n]]
        if len(items) > n:
            lines.append(f"… 그 외 {len(items) - n}개")
    acc = filing["acc"]
    lines += ["", f'<a href="https://www.sec.gov/Archives/edgar/data/{fund["cik"]}/{acc.replace("-", "")}/{acc}-index.htm">SEC 원문</a>']
    return "\n".join(lines)


def main():
    cfg = load_config()["thirteenf"]
    st = load_state("thirteenf")
    seen = set(st.get("seen", []))
    first_run = not seen
    force = "--force" in sys.argv
    errors = []
    for fund in cfg["funds"]:
        try:
            filings = list_13f(fund["cik"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fund['name']}: {e}")
            continue
        if not filings:
            continue
        targets = [f for f in filings if f["acc"] not in seen]
        if force and not targets:
            targets = filings[:1]
        for f in targets:
            seen.add(f["acc"])
        if first_run and not force:
            # 첫 실행: 최근 60일 안에 나온 최신 보고서만 보냄 (지난 분기 것을 한꺼번에 보내지 않도록)
            recent = dt.date.fromisoformat(filings[0]["filed"]) >= dt.date.today() - dt.timedelta(days=60)
            targets = filings[:1] if recent else []
        for f in targets[:1]:  # 같은 펀드에 여러 개가 새로 떴으면 최신 것만
            try:
                cur = holdings(fund["cik"], f["acc"])
                older = [x for x in filings if x["period"] < f["period"]]
                prev_f = older[0] if older else None
                prev = holdings(fund["cik"], prev_f["acc"]) if prev_f else {}
                n = cfg.get("top_n", 15)
                shown = (sorted(cur.values(), key=lambda r: -r["value"])[:n * 4] +
                         sorted((prev[k] for k in prev if k not in cur), key=lambda r: -r["value"])[:n])
                ind = industries(shown, st.setdefault("industry", {}))
                tickers = cusip_to_ticker([r["cusip"] for r in shown], st.setdefault("ticker", {}))
                since = dt.date.fromisoformat(f["period"])
                moves = {r["cusip"]: change_since(tickers.get(r["cusip"]), since) for r in shown}
                send_long(build_message(fund, f, cur, prev, cfg, ind, moves))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{fund['name']}: {e}")
    st["seen"] = sorted(seen)
    save_state("thirteenf", st)
    if errors:
        notify_error_once("13F", " / ".join(errors)[:800])
        sys.exit(1)


if __name__ == "__main__":
    main()
