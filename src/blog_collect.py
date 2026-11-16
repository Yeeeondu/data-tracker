"""블로그 글 수집 — 블로거들의 글(제목·본문)을 state/blog_posts.json 에 모아 둠.

- 처음 실행: 전체 글 목록(제목)을 최대한 모으고, 최근 글은 본문까지 저장
- 매주 일요일 자정: 새 글을 추가로 저장
  (ANTHROPIC_API_KEY가 있으면 이어서 관심사 변화를 분석해 '앞으로 다룰 주제' 검색어를 자동 갱신)
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.parse

import requests

from common import UA, load_config, load_state, save_state
from news import _strip, ask_claude_json, fetch_rss, llm_available

H = {"User-Agent": UA, "Referer": "https://blog.naver.com/"}


def title_list(bid, max_pages=40):
    """블로그 전체 글 제목 목록 (네이버 글 목록 API). 실패하면 빈 목록."""
    out = []
    for page in range(1, max_pages + 1):
        url = (f"https://blog.naver.com/PostTitleListAsync.naver?blogId={bid}&viewdate=&currentPage={page}"
               "&categoryNo=0&parentCategoryNo=&countPerPage=30")
        try:
            r = requests.get(url, headers=H, timeout=30)
            r.raise_for_status()
            data = json.loads(r.text.replace("\\'", "'"))
        except Exception as e:  # noqa: BLE001
            print(f"{bid} 목록 {page}쪽 실패: {e}", file=sys.stderr)
            break
        posts = data.get("postList") or []
        for p in posts:
            out.append({"logNo": str(p.get("logNo")),
                        "title": urllib.parse.unquote_plus(p.get("title", "")).strip(),
                        "date": p.get("addDate", "")})
        if len(posts) < 30:
            break
        time.sleep(0.5)
    return out


def post_body(bid, log_no, limit=4000):
    try:
        r = requests.get(f"https://blog.naver.com/PostView.naver?blogId={bid}&logNo={log_no}",
                         headers=H, timeout=30)
        r.raise_for_status()
        html = r.text
        m = re.search(r'<div class="se-main-container">(.*?)</div>\s*</div>\s*</div>\s*<!--', html, re.S) \
            or re.search(r'<div class="se-main-container">(.*)', html, re.S) \
            or re.search(r'id="postViewArea"(.*)', html, re.S)
        text = _strip(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", m.group(1) if m else html, flags=re.S))
        return text[:limit]
    except Exception as e:  # noqa: BLE001
        print(f"{bid}/{log_no} 본문 실패: {e}", file=sys.stderr)
        return ""


def log_no_from_link(link):
    m = re.search(r"/(\d{9,})", link or "")
    return m.group(1) if m else ""


def collect(ids, db, fetch_bodies=40):
    added = 0
    for bid in ids:
        posts = db.setdefault(bid, {})
        if not posts:  # 처음: 전체 제목 목록
            for p in title_list(bid):
                posts.setdefault(p["logNo"], {"title": p["title"], "date": p["date"], "body": ""})
        try:
            rss = fetch_rss(f"https://rss.blog.naver.com/{bid}.xml")
        except Exception as e:  # noqa: BLE001
            print(f"{bid} RSS 실패: {e}", file=sys.stderr)
            rss = []
        n_body = 0
        for it in rss:
            ln = log_no_from_link(it["link"])
            if not ln:
                continue
            rec = posts.get(ln)
            if rec is None:
                rec = posts[ln] = {"title": it["title"], "date": it["date"].date().isoformat() if it["date"] else "",
                                   "body": ""}
                rec["new"] = dt.date.today().isoformat()
                added += 1
            if not rec.get("body") and n_body < fetch_bodies:
                rec["body"] = post_body(bid, ln) or it["desc"]
                n_body += 1
                time.sleep(0.5)
    return added


def update_profile(db, prof_state):
    """API 키가 있을 때: 최근 1주 글 + 기존 관심사로 '앞으로 다룰 주제' 검색어 갱신."""
    week_ago = (dt.date.today() - dt.timedelta(days=8)).isoformat()
    recent = {bid: [{"title": p["title"], "body": p.get("body", "")[:1200]}
                    for p in posts.values() if p.get("new", "") >= week_ago or p.get("date", "") >= week_ago]
              for bid, posts in db.items()}
    cfg_prof = load_config()["blogs"].get("profile", {})
    prompt = ("내가 즐겨 읽는 투자 블로거들의 관심사 정리와, 이번 주 새로 올라온 글이다.\n"
              f"[기존 관심사]\n{json.dumps(prof_state.get('profile') or cfg_prof, ensure_ascii=False)}\n\n"
              f"[이번 주 글]\n{json.dumps(recent, ensure_ascii=False)[:50000]}\n\n"
              "1) 관심사가 어디로 이동하고 있는지 정리하고, 2) 이들이 '앞으로' 쓸 글에 담길 내용을 예측해, "
              "그 글을 쓰기 전에 읽을 법한 기사를 찾는 구글 뉴스 검색어 20~30개를 만들어라 "
              "(이미 쓴 글의 주제를 반복하는 검색어보다, 다음 단계·파생 이슈·선행 지표 위주). "
              "검색어는 1~4단어, 한국어(lang=ko)와 영어(lang=en)를 섞어라.\n"
              '출력: {"shift": "관심사 이동 2~3문장", "themes": ["..."], "queries": [{"q": "...", "lang": "ko"}]}')
    return ask_claude_json(prompt, max_tokens=3000)


def main():
    cfg = load_config()["blogs"]
    db = load_state("blog_posts")
    added = collect(cfg["ids"], db)
    save_state("blog_posts", db)
    print(f"새 글 {added}개, 총 {sum(len(v) for v in db.values())}개 저장")
    if llm_available() and "--no-llm" not in sys.argv:
        st = load_state("blogs")
        try:
            prof = update_profile(db, st)
            prof["updated"] = dt.date.today().isoformat()
            st["profile"] = prof
            save_state("blogs", st)
        except Exception as e:  # noqa: BLE001
            print(f"관심사 갱신 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
