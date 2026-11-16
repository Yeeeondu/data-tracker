"""매일 06:00 — 블로거들이 '앞으로' 쓸 글에 참고할 만한 기사를 골라 제목·링크만 전송.

검색어(관심사)는 다음 순서로 정함:
 1) ANTHROPIC_API_KEY가 있으면 매주 일요일 blog_collect.py가 자동 갱신한 검색어 (state/blogs.json)
 2) 없으면 config.yaml 의 blogs.profile.queries (블로그 글을 읽고 정리해 둔 검색어)
 3) 그것도 없으면 수집된 글 제목에서 자주 나온 단어
"""
import collections
import datetime as dt
import json
import re
import sys

from common import load_config, load_state, notify_error_once
from news import ask_claude_json, dedupe, google_news, link, llm_available, send_long

STOP = set("""그리고 그러나 하지만 그래서 오늘 이번 지난 관련 대한 위한 통해 있는 없는 하는 했다 합니다 입니다
있습니다 생각 정리 이야기 내용 포스팅 블로그 일상 리뷰 후기 기록 공유 주간 월간 전망 분석 이슈 뉴스 시장""".split())


def pick_profile(cfg):
    st = load_state("blogs").get("profile")
    if st and st.get("queries"):
        return st
    prof = cfg.get("profile") or {}
    if prof.get("queries"):
        return prof
    cnt = collections.Counter()
    for posts in load_state("blog_posts").values():
        for p in list(posts.values())[:200]:
            for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", p.get("title", "")):
                if w not in STOP and not w.isdigit():
                    cnt[w] += 1
    return {"summary": "", "queries": [{"q": w, "lang": "ko"} for w, _ in cnt.most_common(20)]}


def main():
    cfg = load_config()["blogs"]
    prof = pick_profile(cfg)
    if not prof["queries"]:
        notify_error_once("블로거 기사", "검색어가 없음 — '블로그수집'을 먼저 실행해 주세요")
        sys.exit(1)

    cands = []
    for q in prof["queries"]:
        try:
            for i in google_news(q["q"], q.get("lang", "ko"), hours=24)[:10]:
                i["q"] = q["q"]
                cands.append(i)
        except Exception as e:  # noqa: BLE001
            print(f"{q}: {e}", file=sys.stderr)
    cands = dedupe(cands)
    if not cands:
        return  # 보낼 기사가 없으면 조용히 끝냄

    n = cfg["max_articles"]
    picks = None
    if llm_available():
        try:
            lst = [{"id": k, "title": c["title"], "source": c["source"]} for k, c in enumerate(cands[:400])]
            info = {k: prof.get(k) for k in ("summary", "shift", "themes", "next_topics") if prof.get(k)}
            prompt = (f"투자 블로거들의 관심사와 최근 흐름: {json.dumps(info, ensure_ascii=False)}\n\n"
                      f"아래 지난 24시간 기사 중, 이 블로거들이 '다음에 쓸 글'을 준비하며 읽을 가능성이 가장 높은 기사 {n}개를 골라라. "
                      "이미 많이 다룬 주제의 반복보다 새 전개·선행 신호·파생 이슈를 우선하고, 같은 사건은 하나만, "
                      "광고·단순 시세 기사는 제외. 중요도 순.\n"
                      + json.dumps(lst, ensure_ascii=False) + '\n출력: {"ids": [...]}')
            ids = ask_claude_json(prompt, max_tokens=600).get("ids", [])
            picks = [cands[i] for i in ids if isinstance(i, int) and 0 <= i < len(cands)][:n]
        except Exception as e:  # noqa: BLE001
            print(f"AI 선별 실패: {e}", file=sys.stderr)
    if not picks:
        # 검색어마다 최신 기사를 돌아가며 하나씩 → 여러 주제가 고르게 섞이도록
        by_q = collections.defaultdict(list)
        for c in sorted(cands, key=lambda c: -(c["date"].timestamp() if c["date"] else 0)):
            by_q[c["q"]].append(c)
        picks, rnd = [], 0
        while len(picks) < n and any(len(v) > rnd for v in by_q.values()):
            for q in prof["queries"]:
                lst = by_q.get(q["q"], [])
                if len(lst) > rnd and len(picks) < n:
                    picks.append(lst[rnd])
            rnd += 1

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d")
    lines = [f"📚 <b>블로거 관심 기사</b> ({today})"]
    for k, c in enumerate(picks, 1):
        lines.append(f"{k}. {link(c['title'][:110], c['link'])}")
    send_long("\n".join(lines))


if __name__ == "__main__":
    main()
