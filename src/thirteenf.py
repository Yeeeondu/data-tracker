"""분기별 13F — 새 보고서가 SEC에 올라오면 보유 종목별 평가액과 직전 분기 대비 주식 수 변화를 전송."""
import datetime as dt
import re
import os
import sys
import time
import xml.etree.ElementTree as ET

import requests

from common import html_escape, load_config, load_state, notify_error_once, save_state
from news import send_long


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


def label(r, dup=()):
    n = short_name(r["name"])
    if n in dup and share_class(r["name"]):
        n += f" {share_class(r['name'])}"
    return n + ({"CALL": " (Call)", "PUT": " (Put)"}.get(r["putcall"], ""))


def quarter(period):
    y, m = int(period[:4]), int(period[5:7])
    return f"{(m - 1) // 3 + 1}Q{y % 100:02d}"


def pct(x):
    return f"{x:+.0f}%".replace("+0%", "0%").replace("-0%", "0%")


def build_message(fund, filing, cur, prev, cfg):
    """예) - Nvidia Corporation: 평가액 $20.98B, 비중 35.2% (+3.1%p), 주식수 QoQ +6%"""
    n = cfg.get("top_n", 20)
    tot = sum(r["value"] for r in cur.values()) or 1
    ptot = sum(r["value"] for r in prev.values()) or 1
    top = sorted(cur.values(), key=lambda r: -r["value"])
    names = [short_name(r["name"]) for r in list(cur.values()) + list(prev.values())]
    dup = {x for x in names if names.count(x) > 1}
    lines = [f"<b>{html_escape(fund['name'])}, {quarter(filing['period'])} 13F 공시</b>", ""]
    for r in top[:n]:
        key = f"{r['cusip']}|{r['putcall']}"
        w = r["value"] / tot * 100
        p = prev.get(key)
        if not prev:
            wch, sch = "", "-"
        elif p is None:
            wch, sch = " (신규)", "신규"
        else:
            wch = f" ({w - p['value'] / ptot * 100:+.1f}%p)".replace("+0.0%p", "0.0%p").replace("-0.0%p", "0.0%p")
            sch = pct((r["shares"] / p["shares"] - 1) * 100) if p["shares"] > 0 else "-"
        lines.append(f"- {html_escape(label(r, dup))}: 평가액 {money(r['value'])}, 비중 {w:.1f}%{wch}, 주식수 QoQ {sch}")
    if len(top) > n:
        lines.append(f"  (그 외 {len(top) - n}개 종목)")
    if prev:
        gone = sorted((prev[k] for k in prev if k not in cur), key=lambda r: -r["value"])
        for r in gone[:10]:
            lines.append(f"- {html_escape(label(r, dup))}: 전량 매도 (직전 비중 {r['value'] / ptot * 100:.1f}%)")
        if len(gone) > 10:
            lines.append(f"  (그 외 전량 매도 {len(gone) - 10}개 종목)")
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
                send_long(build_message(fund, f, cur, prev, cfg))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{fund['name']}: {e}")
    st["seen"] = sorted(seen)
    save_state("thirteenf", st)
    if errors:
        notify_error_once("13F", " / ".join(errors)[:800])
        sys.exit(1)


if __name__ == "__main__":
    main()
