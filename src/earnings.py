"""어닝콜 AI 체크: 모틀리풀에 올라온 어닝콜 원문을 읽고,
① AI 인프라 진행(순조/방해) ② AI 컴퓨팅 투자 방향(확대/유지/축소) ③ AI로 개선된 지표·비용
에 해당하는 발언을 한국어 번역으로 보내고, 직전 분기 콜과 비교한 내용을 괄호로 붙인다.

python src/earnings.py          # 매일 아침 새 원문 확인 + 밀린 원문 이어서 처리
python src/earnings.py --force  # 이미 처리한 원문도 다시 처리(최근 3일치만)
"""
import datetime as dt
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from common import UA, load_config, load_state, save_state, notify_error_once, html_escape
from common import send_message
from news import ask_claude_json, llm_available, USAGE

BASE = "https://www.fool.com"
LINK_RE = re.compile(r'href="(?:https://www\.fool\.com)?(/earnings/call-transcripts/(\d{4})/(\d{2})/(\d{2})/([^"/]+)/?)"')
SLUG_RE = re.compile(r"^(.*)-([a-z0-9]+)-q([1-4])-(\d{4})-earnings-call-transcript")

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} → {last}")


# ---------- 1. 원문 목록 (월별 사이트맵) ----------
# 목록 페이지는 최근 50쪽(약 2주)까지만 열려서, 월별 사이트맵(/sitemap/YYYY/MM)으로 전체 목록을 받는다.
def parse_list(page_html):
    out, seen = [], set()
    for m in LINK_RE.finditer(page_html.replace("<loc>", 'href="').replace("</loc>", '"')):
        path, y, mo, d, slug = m.groups()
        sm = SLUG_RE.match(slug)
        url = BASE + path.rstrip("/") + "/"
        if not sm or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "date": f"{y}-{mo}-{d}",
                    "ticker": sm.group(2).upper(), "q": f"Q{sm.group(3)} {sm.group(4)}"})
    return out


def months(since):
    y, m = int(since[:4]), int(since[5:7])
    today = dt.date.today()
    while (y, m) <= (today.year, today.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def crawl_index(st, index_from):
    idx = st.setdefault("index", {})
    known = {e["url"] for lst in idx.values() for e in lst}
    done_months = set(st.setdefault("full_months", []))
    added = 0
    today = dt.date.today()
    for y, m in months(index_from):
        key = f"{y}-{m:02d}"
        # 지난달 이전은 한 번 다 받으면 다시 안 받음 (이번 달·지난달은 매번 확인)
        if key in done_months:
            continue
        items = [i for i in parse_list(get(f"{BASE}/sitemap/{y}/{m:02d}")) if i["date"] >= index_from]
        for i in items:
            if i["url"] not in known:
                idx.setdefault(i["ticker"], []).append({"url": i["url"], "date": i["date"], "q": i["q"]})
                known.add(i["url"])
                added += 1
        last = dt.date(y, m, 1)
        if (today.year - last.year) * 12 + today.month - last.month >= 2:
            done_months.add(key)
        time.sleep(1)
    st["full_months"] = sorted(done_months)
    for lst in idx.values():
        lst.sort(key=lambda e: e["date"])
    print(f"색인: 새 원문 {added}개, 전체 {len(known)}개")


# ---------- 2. 원문 읽기 ----------
TAG_RE = re.compile(r"<[^>]+>")
PROMO = re.compile(r"Stock Advisor|See the \d+ stocks|10 best stocks|Motley Fool|fool\.com|"
                   r"before you buy stock|Top 10 Stocks", re.I)


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


def read_transcript(url):
    page = get(url)
    t = re.search(r"<title>(.*?)</title>", page, re.S)
    title = clean(t.group(1)).split("|")[0].strip() if t else url
    company = re.sub(r"\s*\([^)]*\)\s*Q[1-4].*$", "", title).strip() or title
    body_at = page.find('id="article-body"')
    body = page[body_at:] if body_at >= 0 else page
    roles = {}
    pm = re.search(r"CALL PARTICIPANTS</h2>\s*<ul>(.*?)</ul>", body, re.S)
    if pm:
        for li in re.findall(r"<li>(.*?)</li>", pm.group(1), re.S):
            li = clean(li)
            if " - " in li:
                role, name = li.rsplit(" - ", 1)
                roles[name.strip()] = role.strip()
    start = re.search(r"Full Conference Call Transcript", body)
    if start:
        body = body[start.end():]
    paras, speaker = [], ""
    for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S):
        txt = clean(p)
        if len(txt) < 30 or PROMO.search(txt):
            continue
        m = re.match(r"^([A-Z][\w.'\- ]{1,40}):\s+(.*)$", txt)
        if m:
            speaker, txt = m.group(1).strip(), m.group(2)
        paras.append({"speaker": speaker, "text": txt})
    return {"title": title, "company": company, "roles": roles, "paras": paras}


# ---------- 3. 단어로 1차 거르기 ----------
AI = re.compile(r"\bAI\b|artificial intelligence|\bGPUs?\b|inference|\btokens?\b|\bcompute\b|"
                r"\bLLMs?\b|large language|agentic|\bagents?\b|copilot|generative|machine learning|"
                r"data ?cent(er|re)s?|hyperscal|neocloud|\bTPUs?\b|accelerator", re.I)
INFRA = re.compile(r"shortage|constrain|bottleneck|delay|lead times?|skilled (labor|trade|worker)|"
                   r"craft|labor (cost|availability|market)|workforce|permit|interconnect|"
                   r"transformer|turbine|switchgear|power (availability|supply|demand)|\bgrid\b|"
                   r"megawatt|gigawatt|\b[0-9.]+ ?(MW|GW)\b|backlog|capacity (constraint|addition)", re.I)
SPEND = re.compile(r"cap ?ex|capital (expenditure|spending|investment)|cloud (cost|spend)|"
                   r"compute (cost|spend|capacity)|infrastructure (spend|investment|cost)", re.I)


def prefilter(paras, cap=30000):
    keep = []
    for i, p in enumerate(paras):
        t = p["text"]
        if AI.search(t) or SPEND.search(t) or INFRA.search(t):
            keep.append(i)
    out, total = [], 0
    for i in keep:
        total += len(paras[i]["text"])
        if total > cap:
            break
        out.append(i)
    return out


def numbered(paras, ids, roles):
    lines = []
    for i in ids:
        p = paras[i]
        who = p["speaker"] + (f" ({roles[p['speaker']]})" if p["speaker"] in roles else "")
        lines.append(f"[{i}] {who}: {p['text']}")
    return "\n".join(lines)


# ---------- 4. Claude 판단 ----------
CRITERIA = """찾는 내용은 3가지입니다.
① AI 인프라 투자 진행: 데이터센터·전력·네트워크·반도체 공급 등 AI 인프라 구축이 순조로운지, 방해 요인이 있는지.
   AI를 직접 말하지 않아도 데이터센터 건설·전력 설비에 영향을 주는 병목이면 포함.
   예) 건설 현장 숙련공 부족으로 비용 상승, 변압기·가스터빈 납기 지연, 전력 연결 대기, 부품 품귀, 반대로 '공급 차질 해소·수주 가속'.
② AI 컴퓨팅 투자 방향: 회사가 AI 컴퓨팅(GPU·클라우드·토큰·추론 비용·CAPEX)에 쓰는 돈을 늘리는지, 유지하는지, 줄이는지.
   예) 핀터레스트가 컴퓨팅 투자를 더 늘린다, 아마존이 보안 용도 토큰·비용 증가를 예상, 빅테크가 추론 비용을 낮춰 가겠다.
③ AI로 개선(또는 악화)된 성과지표·비용: AI를 써서 생산성·전환율·고객지원 비용·개발 속도·인력 등 구체적 지표가 바뀐 사례.
   예) 에어비앤비 고객지원 AI로 상담 비용 감소, 듀오링고 AI로 콘텐츠 제작 속도 향상, 유니티 AI 광고로 성과 개선.
제외: 구체적 근거 없는 AI 기대감('AI 기회가 크다'), 단순 제품 소개, 매출 중 AI 비중 같은 일반 실적 숫자만 있는 문장,
     애널리스트 질문 자체(답변만 봄), 회사와 무관한 일반론."""


def screen(text, model):
    res = ask_claude_json(
        f"{CRITERIA}\n\n아래는 어닝콜 원문에서 단어로 추린 문단입니다(번호 포함).\n"
        "위 3가지 중 하나에 확실히 해당하는 '경영진 발언' 문단 번호만 고르세요. 없으면 빈 목록.\n"
        '형식: {"ids": [번호, ...]}\n\n' + text, max_tokens=300, model=model)
    return [int(i) for i in (res.get("ids") or []) if str(i).isdigit()]


def extract(cur, cur_text, prev, prev_text, model):
    prev_part = (f"\n\n[직전 분기 콜: {prev['q']}, {prev['date']}]\n{prev_text}" if prev_text
                 else "\n\n[직전 분기 콜 원문 없음]")
    res = ask_claude_json(
        f"{CRITERIA}\n\n회사: {cur['company']}\n[이번 콜: {cur['q']}, {cur['date']}]\n{cur_text}{prev_part}\n\n"
        "이번 콜에서 3가지 중 하나에 해당하는 발언을 뽑으세요. 같은 이야기는 하나로 합치고 최대 5개.\n"
        "- topic: 1, 2, 3 중 하나\n"
        "- tag: ①이면 '순조' 또는 '방해', ②이면 '확대'·'유지'·'축소', ③이면 '개선' 또는 '악화'\n"
        "- who: 말한 사람 이름과 직책(짧게, 예: 'Marvin Ellison CEO')\n"
        "- ko: 원문 발언을 충실하게 한국어로 번역(요약 금지, 핵심 문장만 잘라 2~4문장 이내, 숫자·단위 그대로)\n"
        "- compare: 수치·방향(상향/하향/개선/악화/확대 등)이 있는 발언이면 직전 분기 콜에서 같은 항목을 어떻게 말했는지 비교.\n"
        "  예: '직전 분기(8/5)에는 CAPEX $190B 예상 → 이번에 $210B로 상향', '직전 분기에는 생산성 +20% 개선 예상'.\n"
        "  직전 분기 원문에 같은 항목이 없으면 '직전 분기 콜에는 관련 언급 없음'. 원문 자체가 없으면 ''.\n"
        "  직전 분기 원문에 없는 숫자를 지어내지 마세요.\n"
        '형식: {"items": [{"topic": 1, "tag": "방해", "who": "...", "ko": "...", "compare": "..."}]}',
        max_tokens=2500, model=model)
    return [i for i in (res.get("items") or []) if i.get("ko") and str(i.get("topic")) in "123"]


def prev_call(st, ticker, date):
    lst = [e for e in st.get("index", {}).get(ticker, []) if e["date"] < date]
    lst = [e for e in lst if (dt.date.fromisoformat(date) - dt.date.fromisoformat(e["date"])).days >= 45]
    return lst[-1] if lst else None


def process(entry, st, cfg):
    """한 원문 처리. 결과: None(해당 없음) 또는 메시지에 쓸 dict."""
    tr = read_transcript(entry["url"])
    if not tr["paras"]:
        raise RuntimeError(f"본문을 못 읽음: {entry['url']}")
    ids = prefilter(tr["paras"])
    if not ids:
        return None
    hits = screen(numbered(tr["paras"], ids, tr["roles"]), cfg["screen_model"])
    hits = [i for i in hits if 0 <= i < len(tr["paras"])]
    if not hits:
        return None
    ctx = sorted({j for i in hits for j in (i - 1, i, i + 1) if 0 <= j < len(tr["paras"])})
    cur = {"company": tr["company"], "q": entry["q"], "date": entry["date"]}
    cur_text = numbered(tr["paras"], ctx, tr["roles"])
    prev, prev_text = prev_call(st, entry["ticker"], entry["date"]), ""
    if prev:
        try:
            pt = read_transcript(prev["url"])
            prev_text = numbered(pt["paras"], prefilter(pt["paras"], cap=15000), pt["roles"])
        except Exception as e:
            print(f"직전 분기 원문 실패 {prev['url']}: {e}", file=sys.stderr)
    items = extract(cur, cur_text, prev, prev_text, cfg["model"])
    if not items:
        return None
    return {"company": tr["company"], "ticker": entry["ticker"], "q": entry["q"],
            "date": entry["date"], "url": entry["url"], "items": items}


# ---------- 5. 메시지 ----------
TOPIC = {"1": "①AI 인프라", "2": "②AI 컴퓨팅 투자", "3": "③AI 효과"}
MARK = {"방해": "🔴", "악화": "🔴", "축소": "🔻", "순조": "🟢", "개선": "🟢", "확대": "🔺", "유지": "⚪"}


def render(r):
    d = dt.date.fromisoformat(r["date"])
    lines = [f'🎙 <b>{html_escape(r["company"])} ({r["ticker"]})</b> · {r["q"]} 콜 · {d.month}/{d.day}']
    for it in sorted(r["items"], key=lambda x: str(x.get("topic"))):
        tag = it.get("tag", "")
        head = f'{MARK.get(tag, "•")} {TOPIC.get(str(it["topic"]), "")} {tag}'
        who = f' — {html_escape(it["who"])}' if it.get("who") else ""
        lines.append(f'{head}: “{html_escape(it["ko"])}”{who}')
        if it.get("compare"):
            lines.append(f'   ({html_escape(it["compare"])})')
    lines.append(f'<a href="{html_escape(r["url"])}">원문</a>')
    return "\n".join(lines)


def quarter_key(d=None):
    d = d or dt.date.today()
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def run_cost(cfg):
    price = cfg.get("price_per_mtok", {})
    return sum(i / 1e6 * price.get(m, [3, 15])[0] + o / 1e6 * price.get(m, [3, 15])[1]
               for m, (i, o) in USAGE.items())


def send_blocks(head, blocks, silent=False, limit=3800):
    """회사 단위가 중간에 잘리지 않게 묶어서 보냄."""
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = ""
        cur = (cur + "\n\n" + b) if cur else b
    if cur:
        msgs.append(cur)
    for m in msgs:
        send_message(m[:4000], silent=silent, limit=None)
        time.sleep(1.2)


# ---------- main ----------
def main():
    force = "--force" in sys.argv
    cfg_all = load_config()
    cfg = cfg_all["earnings"]
    if not llm_available():
        print("ANTHROPIC_API_KEY 없음 → 중단")
        return
    st = load_state("earnings")
    st.setdefault("done", {})
    t0 = time.time()
    budget = cfg.get("budget_min", 45) * 60
    try:
        crawl_index(st, cfg["index_from"])
    except Exception as e:
        save_state("earnings", st)
        notify_error_once("어닝콜 목록", e)
        raise
    save_state("earnings", st)

    entries = [e | {"ticker": t} for t, lst in st["index"].items() for e in lst
               if e["date"] >= cfg["season_start"]]
    if force:
        recent = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        for e in entries:
            if e["date"] >= recent:
                st["done"].pop(e["url"], None)
    todo = sorted([e for e in entries if e["url"] not in st["done"]], key=lambda e: e["date"])
    print(f"처리할 원문 {len(todo)}개")

    results, fails, ok = [], 0, 0
    qk = quarter_key()
    spend = st.setdefault("spend", {})
    spent0 = spend.get(qk, 0.0)
    cap = cfg.get("max_usd_per_quarter", 80)
    if spent0 >= cap:
        print(f"이번 분기 이용료 ${spent0:.2f} ≥ 한도 ${cap} → 처리 안 함")
        todo = []
    with ThreadPoolExecutor(cfg.get("workers", 4)) as ex:
        pending = list(todo)
        running = {}
        while pending or running:
            over = spent0 + run_cost(cfg) >= cap
            while pending and not over and len(running) < cfg.get("workers", 4) and time.time() - t0 < budget:
                e = pending.pop(0)
                running[ex.submit(process, e, st, cfg)] = e
            if not running:
                break
            fut = next(as_completed(running))
            e = running.pop(fut)
            try:
                r = fut.result()
                st["done"][e["url"]] = e["date"]
                ok += 1
                if r:
                    results.append(r)
                    print(f"  ✔ {e['ticker']} {e['date']}: {len(r['items'])}건")
            except Exception as err:
                fails += 1
                fc = st.setdefault("fail", {})
                fc[e["url"]] = fc.get(e["url"], 0) + 1
                if fc[e["url"]] >= 3:          # 3번 실패하면 건너뜀
                    st["done"][e["url"]] = e["date"]
                print(f"  ✘ {e['ticker']} {e['url']}: {err}", file=sys.stderr)
                if fails >= 15 and ok == 0:
                    notify_error_once("어닝콜 원문", err)
                    break
            if ok and ok % 20 == 0:
                save_state("earnings", st)
    save_state("earnings", st)
    left = len([e for e in todo if e["url"] not in st["done"]])
    print(f"이번 실행: 원문 {len(todo) - left}개 처리, 해당 {len(results)}개사, 실패 {fails}, 남은 원문 {left}개")

    for m, (i, o) in USAGE.items():
        print(f"토큰 사용 {m}: 입력 {i:,} / 출력 {o:,}")
    cost = run_cost(cfg)
    spend[qk] = round(spent0 + cost, 4)
    save_state("earnings", st)
    print(f"이번 실행 이용료 약 ${cost:.2f}, {qk} 누적 약 ${spend[qk]:.2f} (한도 ${cap})")
    if spend[qk] >= cap and spent0 < cap:
        send_message(f"⚠️ 어닝콜 AI 체크: {qk} Claude 이용료가 한도 ${cap}에 닿아 이번 분기는 멈췄어요. "
                     f"남은 원문 {left}개. 계속하려면 config.yaml의 max_usd_per_quarter를 올려 주세요.")
    if results:
        results.sort(key=lambda r: (r["date"], r["company"]))
        old = (dt.date.today() - dt.timedelta(days=4)).isoformat()
        backlog = all(r["date"] < old for r in results)
        head = f"🎙 <b>어닝콜 AI 체크</b> — {len(results)}개사" + (" (지난 콜 채우는 중)" if backlog else "")
        send_blocks(head, [render(r) for r in results], silent=backlog)


if __name__ == "__main__":
    main()
