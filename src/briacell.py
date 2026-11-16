"""브리아셀 보도자료 감시(3시간마다) → 3상 관련 보도자료만 텔레그램 알림, 탑라인은 🚨 표시.

소스 1: BriaCell IR 뉴스 위젯(issuerdirect) — 홈페이지와 같은 목록
소스 2: 구글 뉴스 RSS — 위젯이 막히거나 늦을 때 백업
"""
import datetime as dt
import email.utils
import re
import sys
import urllib.parse

import xml.etree.ElementTree as ET

import requests

from common import (UA, browser_page, html_escape, load_config, load_state,
                    notify_error_once, save_state, send_message, sha)

IR_PAGE = "https://investors.briacell.com/news-events"
FRESH_HOURS = 72  # 발표된 지 이 시간 이내인 소식만 알림 (예전 기사 재등장 방지)


def parse_date(s):
    """IR 위젯 '11/6/26 23:00' 또는 RSS 'Mon, 14 Sep 2026 07:00:00 GMT' → UTC datetime. 실패하면 None."""
    s = (s or "").strip()
    for fmt in ("%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def norm_key(title):
    """제목 비교용: 괄호 안 내용·기호·공백을 없애 출처마다 조금 다른 제목도 같은 것으로 봄."""
    t = re.sub(r"\([^)]*\)", "", title.lower())
    t = re.sub(r"\(tm\)|™|®", "", t)
    return sha(re.sub(r"[^a-z0-9]+", "", t))


def fetch_ir(feed_url):
    year = dt.date.today().year
    url = f"{feed_url}&starttime={year - 1}-01-01&endtime={year}-12-31"
    with browser_page(locale="en-US") as page:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_selector("#news-table tbody tr .headline", timeout=30000)
        rows = page.eval_on_selector_all(
            "#news-table tbody tr",
            """rs => rs.map(r => {
                const h = r.querySelector('.headline:not([style*="none"])') || r.querySelector('.headline');
                const lines = r.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
                return {title: (h ? h.innerText : lines[0] || '').trim(), date: lines[1] || ''};
            })""")
    items = [{"title": r["title"], "date": r["date"], "link": IR_PAGE, "src": "IR"}
             for r in rows if r["title"]]
    if not items:
        raise RuntimeError("IR 위젯에서 보도자료를 하나도 못 읽음 (구조 변경 가능성)")
    return items


def fetch_gnews(query):
    q = urllib.parse.quote(query)
    # when:3d — 최근 3일 기사만 검색
    r = requests.get(f"https://news.google.com/rss/search?q={q}%20when%3A3d&hl=en-US&gl=US&ceid=US:en",
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return [{"title": i.findtext("title", ""), "date": i.findtext("pubDate", ""),
             "link": i.findtext("link", ""), "src": "Google News"} for i in root.iter("item")]


def classify(title, cfg):
    t = title.lower()
    is_top = any(re.search(p, t) for p in cfg["topline_patterns"])
    is_preview = any(re.search(p, t) for p in cfg["preview_patterns"])
    if is_top and not is_preview:
        return "topline"
    if any(re.search(p, t) for p in cfg.get("phase3_patterns") or []):
        return "phase3"
    return None


def main():
    cfg = load_config()["briacell"]
    st = load_state("briacell")
    seen = set(st.get("seen", []))
    first_run = not seen

    items, errors = [], []
    try:
        items += fetch_ir(cfg["feed_url"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"IR 위젯: {e}")
    try:
        items += fetch_gnews(cfg["google_news_query"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"구글 뉴스: {e}")
    if not items:
        notify_error_once("브리아셀", " / ".join(errors))
        sys.exit(1)

    now = dt.datetime.now(dt.timezone.utc)
    for it in items:
        # 구글 뉴스는 ' - 매체명' 꼬리 제거
        norm = re.sub(r"\s+-\s+[^-]+$", "", it["title"]) if it["src"] == "Google News" else it["title"]
        key = norm_key(norm)
        if key in seen:
            continue
        seen.add(key)
        if first_run:
            continue
        # 날짜를 모르거나 발표된 지 오래된 소식은 알리지 않음 (기록만 함)
        when = parse_date(it["date"])
        if when is None or now - when > dt.timedelta(hours=FRESH_HOURS):
            print(f"오래된 소식이라 건너뜀: {it['date']} {norm}")
            continue
        kind = classify(norm, cfg)
        kst = when.astimezone(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M (한국)")
        if kind == "topline":
            send_message(
                "🚨🚨 <b>브리아셀 탑라인 관련 발표 감지</b>\n\n"
                f"<b>{html_escape(norm)}</b>\n{kst} · {it['src']}\n\n"
                f"{html_escape(it['link'])}\nIR: {IR_PAGE}", preview=True)
        elif kind == "phase3":
            send_message(f"📰 브리아셀 3상 관련 보도자료\n{html_escape(norm)}\n{kst} · {it['src']}\n"
                         f"{html_escape(it['link'])}")

    if first_run:
        latest = next((i for i in items if i["src"] == "IR"), items[0])
        send_message("✅ 브리아셀 탑라인 감시를 시작했어요.\n"
                     f"현재 최신 보도자료: {html_escape(latest['title'])} ({html_escape(latest['date'])})",
                     silent=True)
    st["seen"] = sorted(seen)
    save_state("briacell", st)


if __name__ == "__main__":
    main()
