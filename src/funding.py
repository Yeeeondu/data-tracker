"""빅테크·AI 기업 자금 조달 기록.

매주 일요일 밤: 지난 7일 새 발행(회사채·전환사채·주식·지분투자)을 찾아 기록에 더하고,
새 건이 있는 회사만 아래 형식으로 보냄 (없으면 안 보냄).

  • Google: 2025년 373억달러 → 2026년 1,300억달러
     2026년: 회사채 1,300억달러
     ◦ 🆕 26.11.02. Google 회사채 60억유로 발행 결정.
     ...
     지난 합계: 2024년 110억달러(회사채) / 2023년 ...

- 합계는 기록된 발행 금액을 달러로 환산해 코드가 직접 계산 (config.yaml 의 fx 환율 사용)
- 올해·작년은 세부 내역까지, 그보다 오래된 해는 종류별 합계만
- '계획 발표'·'조달 논의'는 기록·표시만 하고 합계에는 넣지 않음
- 비상장사(OpenAI, 상장 전 Anthropic)는 기업가치를 알기 위해 '조달 논의' 단계도 기업가치와 함께 기록

처음 한 번: `python src/funding.py --backfill` → SEC 공시(2020년~)를 훑어 과거 발행 기록을 채움.
"""
import datetime as dt
import html
import json
import re
import sys

from common import html_escape, load_config, load_state, save_state
from news import ask_claude_json, google_news, llm_available, send_long
from thirteenf import sec_get

COUNTED = {"회사채", "전환사채", "보통주", "우선주", "IPO", "지분투자"}
FORMS = re.compile(r"^(FWP|424B[1-8]|8-K|6-K)$")
ITEMS_8K = {"1.01", "2.03", "3.02", "8.01"}


# ───────── SEC ─────────
def doc_text(cik, acc, doc, limit):
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
    try:
        raw = sec_get(url, as_json=False)
    except Exception as e:  # noqa: BLE001
        return url, f"(본문을 못 읽음: {e})"
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return url, re.sub(r"\s+", " ", text).strip()[:limit]


def _rows(block):
    n = len(block["form"])
    items = block.get("items") or [""] * n
    for i in range(n):
        yield {"form": block["form"][i], "date": block["filingDate"][i], "items": items[i] or "",
               "acc": block["accessionNumber"][i], "doc": block["primaryDocument"][i]}


def sec_filings(company, since, until=None, limits=None, history=False):
    if not company.get("cik"):
        return []
    limits = limits or {"FWP": 7000, "8-K": 3500, "6-K": 3500}
    j = sec_get(f"https://data.sec.gov/submissions/CIK{int(company['cik']):010d}.json")
    rows = list(_rows(j["filings"]["recent"]))
    if history:  # 오래된 공시는 별도 파일에 나뉘어 있음
        for f in j["filings"].get("files", []):
            if f.get("filingTo", "9999") >= since:
                rows += list(_rows(sec_get(f"https://data.sec.gov/submissions/{f['name']}")))
    out = []
    for r in rows:
        if r["date"] < since or (until and r["date"] > until) or not FORMS.match(r["form"]):
            continue
        if r["form"] == "8-K" and not (set(r["items"].split(",")) & ITEMS_8K):
            continue
        url, text = doc_text(company["cik"], r["acc"], r["doc"], limits.get(r["form"], 3500))
        out.append({"form": r["form"], "date": r["date"], "url": url, "text": text})
    return sorted(out, key=lambda r: r["date"])


def news(company, days=7):
    q = (f'"{company["news"]}" (bond OR notes OR "debt offering" OR convertible OR "share sale" OR '
         '"stock offering" OR "equity offering" OR funding OR valuation OR raises)')
    try:
        return [{"title": i["title"], "source": i["source"], "link": i["link"],
                 "date": i["date"].date().isoformat() if i["date"] else ""}
                for i in google_news(q, "en", hours=24 * days)[:15]]
    except Exception as e:  # noqa: BLE001
        print(f"{company['name']} 뉴스 실패: {e}", file=sys.stderr)
        return []


# ───────── 기록 ─────────
def is_private(company, today):
    if company.get("private"):
        return True
    until = company.get("private_until")
    return bool(until and today.isoformat() < until)


def usd_eok(deal, fx):
    if deal.get("usd") is not None:
        return float(deal["usd"])
    try:
        return float(deal.get("amt") or 0) * float(fx.get((deal.get("cur") or "USD").upper(), 0))
    except (TypeError, ValueError):
        return 0.0


def deal_year(deal):
    m = re.match(r"(\d{2})\.", deal.get("date", ""))
    return 2000 + int(m.group(1)) if m else None


def same_deal(a, b):
    """같은 발행인지 (날짜 7일 이내 + 종류·통화 같음 + 금액 5% 이내)."""
    if (a.get("date"), a.get("text")) == (b.get("date"), b.get("text")):
        return True
    if a.get("kind") != b.get("kind") or (a.get("cur") or "").upper() != (b.get("cur") or "").upper():
        return False
    try:
        da = dt.datetime.strptime(a["date"][:8], "%y.%m.%d")
        db = dt.datetime.strptime(b["date"][:8], "%y.%m.%d")
        if abs((da - db).days) > 7:
            return False
        x, y = float(a.get("amt") or 0), float(b.get("amt") or 0)
        return abs(x - y) <= 0.05 * max(x, y, 1)
    except (ValueError, KeyError, TypeError):
        return False


def ledger_for(name, cfg, st):
    """config의 직접 정리한 기록 + 자동으로 쌓인 기록(state)을 합쳐 최신순으로."""
    seed = list(((cfg.get("ledger") or {}).get(name) or {}).get("deals", []))
    merged = list(seed)
    for d in (st.get(name) or {}).get("deals", []):
        if not any(same_deal(d, s) for s in seed):
            merged.append(d)
    return sorted(merged, key=lambda d: d.get("date", ""), reverse=True)


def totals(deals, fx):
    by_year = {}
    for d in deals:
        y = deal_year(d)
        if not y:
            continue
        for part in d.get("parts") or [d]:  # 한 줄에 여러 발행이 묶인 경우 parts로 합산
            if part.get("kind") not in COUNTED:
                continue
            kinds = by_year.setdefault(y, {})
            k = {"보통주": "주식", "우선주": "주식", "IPO": "주식"}.get(part["kind"], part["kind"])
            kinds[k] = kinds.get(k, 0) + usd_eok(part, fx)
    return by_year


def fmt_eok(x):
    x = round(float(x), 1)
    if x >= 10000:
        return f"{x / 10000:.2f}".rstrip("0").rstrip(".") + "조달러"
    return f"{x:,.1f}".rstrip("0").rstrip(".") + "억달러"


def breakdown(kinds):
    order = ["회사채", "전환사채", "주식", "지분투자"]
    ks = sorted((k for k in kinds if kinds[k]), key=lambda k: order.index(k) if k in order else 9)
    return " · ".join(f"{k} {fmt_eok(kinds[k])}" for k in ks)


def render(name, deals, fx, today, new_keys=(), note=""):
    year = today.year
    tot = totals(deals, fx)
    recent_years = [y for y in (year - 1, year) if y in tot]
    head = " → ".join(f"{y}년 {fmt_eok(sum(tot[y].values()))}" for y in recent_years)
    if note:
        head = f"{head} → {note}" if head else note
    lines = [f"• <b>{html_escape(name)}</b>" + (f": {html_escape(head)}" if head else "")]
    for y in recent_years:
        lines.append(f"   {y}년 구성: {html_escape(breakdown(tot[y]))}")
    for d in deals:
        y = deal_year(d)
        if y is not None and y < year - 1:
            continue  # 2년 전 이전은 합계만
        mark = "🆕 " if (d.get("date"), d.get("text")) in new_keys else ""
        date = f"{html_escape(d['date'])}. " if d.get("date") else ""
        lines.append(f"   ◦ {mark}{date}{html_escape(d.get('text', ''))}")
        for sub in d.get("sub") or []:
            lines.append(f"       - {html_escape(sub)}")
    old = sorted((y for y in tot if y < year - 1), reverse=True)
    if old:
        lines.append("   지난 합계")
        for y in old:
            lines.append(f"       - {y}년 {fmt_eok(sum(tot[y].values()))} ({html_escape(breakdown(tot[y]))})")
    return "\n".join(lines)


# ───────── AI 추출 ─────────
STYLE = """문장 예시 (날짜는 YY.MM.DD, 금액은 억 단위 + 통화):
 - "Google 회사채 85억캐나다달러 발행 결정, 2031·2033·2036·2056년 만기. 금리 3.650·4.000·4.350·5.000%"
 - "Amazon 42.5억파운드 발행 결정. 3년물 12.5억파운드, 6·12·19년물 각 10억 파운드."
 - "Meta 의무전환우선주 125억달러 발행. 연 배당률 6.50%"
 - "SpaceX 상장으로 862.5억달러 조달."
 - 비상장사: "650억달러 조달, 기업가치 9,650억달러 평가" + 세부 줄 "Altimeter·Dragoneer 주도"

각 발행 JSON: {"date": "YY.MM.DD", "kind": "회사채|전환사채|보통주|우선주|IPO|지분투자|계획|논의",
 "cur": "USD|EUR|JPY|GBP|CAD|AUD|CHF 등 통화코드", "amt": 해당 통화 기준 금액(억 단위 숫자, 예: 60억유로 → 60),
 "text": "위 예시 형식 한 줄", "sub": ["필요할 때만 세부 줄"]}
 - kind: 보통주 선도매각·ATM·유상증자 = 보통주, 의무전환우선주 = 우선주, 비상장사 투자 유치 = 지분투자,
   '조달 계획 발표' = 계획, 비상장사의 '조달 논의·추진' 보도 = 논의 (계획·논의는 합계에 안 들어감)
 - 포함: 회사채(외화채 포함), 전환·교환사채, 보통주·우선주 발행, 상장 공모, 비상장사 투자 유치
 - 제외: 은행 대출, 신용한도, 금융리스, 자사주 매입, 회사로 돈이 안 들어오는 구주 매출, 선반등록만 한 경우, 기존 채권 교환
 - 같은 발행을 다룬 공시가 여러 개면 하나로. 숫자는 공시 우선, 뉴스에만 있으면 text 끝에 '(보도 기준)'."""


def private_rule(company, today):
    if company.get("private"):
        return ("이 회사는 비상장사다. 투자 유치는 '조달 논의' 단계도 kind=논의로 반드시 기록하고, "
                "논의되는 기업가치를 text에 꼭 적어라. 결정되면 kind=지분투자로 따로 기록하라.")
    if is_private(company, today):
        return "이 회사는 아직 상장 전이다. 조달 논의 단계도 기업가치와 함께 기록하라."
    return "상장사다. 같은 조달은 논의·결정 단계를 나누지 말고 '결정(가격 결정·발행 완료)' 내용 하나만 기록하라."


def extract_week(company, fil, nw, existing, today, since):
    prompt = (f"{company['name']}의 지난 7일({since}~{today}) SEC 공시 발췌와 뉴스 제목, 그리고 이미 기록된 최근 발행이다.\n"
              "이미 기록된 것과 겹치지 않는 새 자금 조달만 JSON 배열로 출력해라. 없으면 [].\n"
              f"{private_rule(company, today)}\n\n{STYLE}\n\n"
              + json.dumps({"sec_filings": fil, "news": nw, "already_recorded": existing}, ensure_ascii=False)[:120000])
    return ask_claude_json(prompt, max_tokens=3000) or []


def extract_history(company, filings, existing):
    prompt = (f"{company['name']}의 과거 SEC 공시 발췌다(시간순). 실제로 발행된 자금 조달을 모두 JSON 배열로 출력해라. "
              "이미 기록된 것과 같은 발행은 빼라.\n"
              f"{STYLE}\n\n" + json.dumps({"sec_filings": filings, "already_recorded": existing}, ensure_ascii=False))
    return ask_claude_json(prompt, max_tokens=8000) or []


def clean(d):
    return {"date": str(d.get("date", "")).rstrip("."), "kind": d.get("kind", ""), "cur": (d.get("cur") or "USD").upper(),
            "amt": d.get("amt"), "text": d.get("text", ""), "sub": d.get("sub") or []}


# ───────── 실행 ─────────
def backfill(cfg, st):
    """처음 한 번: SEC 공시를 훑어 과거 기록을 채움 (메시지는 안 보냄)."""
    since = cfg.get("backfill_from", "2020-01-01")
    limits = {"FWP": 3000, "8-K": 2500, "6-K": 2500, **{f"424B{i}": 2000 for i in range(1, 9)}}
    for c in cfg["companies"]:
        if not c.get("cik"):
            continue
        try:
            fil = sec_filings(c, since, limits=limits, history=True)
        except Exception as e:  # noqa: BLE001
            print(f"{c['name']} 실패: {e}", file=sys.stderr)
            continue
        print(f"{c['name']}: 관련 공시 {len(fil)}건")
        rec = st.setdefault(c["name"], {"deals": []})
        chunks, cur, size = [], [], 0
        for f in fil:
            if cur and size + len(f["text"]) > 60000:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(f)
            size += len(f["text"])
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            existing = [f"{d['date']}. {d['text']}" for d in ledger_for(c["name"], cfg, st)][:80]
            try:
                for d in extract_history(c, chunk, existing):
                    d = clean(d)
                    if d["text"] and not any(same_deal(d, x) for x in ledger_for(c["name"], cfg, st)):
                        rec["deals"].append(d)
            except Exception as e:  # noqa: BLE001
                print(f"{c['name']} 추출 실패: {e}", file=sys.stderr)
        save_state("funding", st)
        print(f"{c['name']}: 기록 {len(ledger_for(c['name'], cfg, st))}건")
    st["_backfilled"] = dt.date.today().isoformat()
    save_state("funding", st)


def main():
    cfg = load_config()["funding"]
    fx = cfg.get("fx", {"USD": 1})
    st = load_state("funding")
    if not llm_available():
        print("ANTHROPIC_API_KEY가 없어 자금 조달 정리를 건너뜀")
        return
    if "--backfill" in sys.argv:
        backfill(cfg, st)
        return
    today = dt.date.today()
    since = (today - dt.timedelta(days=7)).isoformat()
    new_keys, changed, sources = set(), [], {}
    for c in cfg["companies"]:
        try:
            fil = sec_filings(c, since)
        except Exception as e:  # noqa: BLE001
            print(f"{c['name']} SEC 실패: {e}", file=sys.stderr)
            fil = []
        nw = news(c)
        if not fil and not nw:
            continue
        existing = [f"{d['date']}. {d['text']}" for d in ledger_for(c["name"], cfg, st)[:10]]
        try:
            found = extract_week(c, fil, nw, existing, today, since)
        except Exception as e:  # noqa: BLE001
            print(f"{c['name']} 추출 실패: {e}", file=sys.stderr)
            continue
        rec = st.setdefault(c["name"], {"deals": []})
        for d in found:
            d = clean(d)
            if not d["text"] or any(same_deal(d, x) for x in ledger_for(c["name"], cfg, st)):
                continue
            d["recorded"] = today.isoformat()
            rec["deals"].append(d)
            new_keys.add((d["date"], d["text"]))
            if c["name"] not in changed:
                changed.append(c["name"])
        sources[c["name"]] = [f["url"] for f in fil if f["form"] == "FWP"][:1] + [n["link"] for n in nw][:1]
    if not changed:
        print("이번 주 새 발행 없음 → 메시지 안 보냄")
        return
    save_state("funding", st)
    blocks = [f"💵 <b>자금 조달 업데이트</b> ({since}~{today})"]
    for name in changed:
        note = ((cfg.get("ledger") or {}).get(name) or {}).get("note", "")
        b = render(name, ledger_for(name, cfg, st), fx, today, new_keys, note)
        if sources.get(name):
            b += "\n   " + " ".join(f'<a href="{html_escape(u)}">원문{k + 1}</a>' for k, u in enumerate(sources[name]))
        blocks.append(b)
    send_long("\n\n".join(blocks))


if __name__ == "__main__":
    main()
