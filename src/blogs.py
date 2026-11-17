"""블로거 관심 기사 고르기 (blog_collect.py 가 매주 일요일 밤에 사용).

글을 쉰 블로거의 관심사 검색어로 지난 7일 기사를 모은 뒤,
그 블로거가 다음 글을 준비하며 읽었을 법한 기사만 골라 제목·링크로 보냄.
"""
import collections
import datetime as dt
import json
import sys

from common import html_escape
from news import ask_claude_json, dedupe, google_news, link, llm_available, send_long


def queries_for(blog_ids, ai_prof, cfg_prof):
    """해당 블로거들의 검색어: AI가 갱신한 것(블로그 표시가 있는 것) 우선, 없으면 config 기본값."""
    qs = [q for q in (ai_prof or {}).get("queries", []) if q.get("blog") in blog_ids]
    if not qs:
        qs = [q for q in (cfg_prof or {}).get("queries", []) if q.get("blog") in blog_ids]
    return qs


def send_for_silent(silent, ai_prof, cfg_prof, n=15):
    qs = queries_for(set(silent), ai_prof, cfg_prof)
    if not qs:
        print(f"검색어 없음: {silent}", file=sys.stderr)
        return
    cands = []
    for q in qs:
        try:
            for i in google_news(q["q"], q.get("lang", "ko"), hours=24 * 7)[:10]:
                i["q"] = q["q"]
                cands.append(i)
        except Exception as e:  # noqa: BLE001
            print(f"{q}: {e}", file=sys.stderr)
    cands = dedupe(cands)
    if not cands:
        return

    picks = None
    if llm_available():
        info = {"블로거별 관심사": {b: (ai_prof or {}).get("by_blog", {}).get(b) for b in silent},
                "전체 요약": (cfg_prof or {}).get("summary", ""),
                "예상 주제": (ai_prof or {}).get("next_topics") or (cfg_prof or {}).get("next_topics")}
        lst = [{"id": k, "title": c["title"], "source": c["source"]} for k, c in enumerate(cands[:400])]
        prompt = (f"이번 주 글을 올리지 않은 투자 블로거({', '.join(silent)})의 관심사: {json.dumps(info, ensure_ascii=False)}\n\n"
                  f"이 블로거들이 계속 글을 썼다면 읽고 참고했을 지난 7일 기사 {n}개를 골라라. "
                  "새 전개·선행 신호·파생 이슈를 우선하고, 같은 사건은 하나만, 광고·단순 시세 기사는 제외. 중요도 순.\n"
                  + json.dumps(lst, ensure_ascii=False) + '\n출력: {"ids": [...]}')
        try:
            ids = ask_claude_json(prompt, max_tokens=600).get("ids", [])
            picks = [cands[i] for i in ids if isinstance(i, int) and 0 <= i < len(cands)][:n]
        except Exception as e:  # noqa: BLE001
            print(f"AI 선별 실패: {e}", file=sys.stderr)
    if not picks:  # 키가 없거나 실패: 검색어마다 최신 기사를 돌아가며
        by_q = collections.defaultdict(list)
        for c in sorted(cands, key=lambda c: -(c["date"].timestamp() if c["date"] else 0)):
            by_q[c["q"]].append(c)
        picks, rnd = [], 0
        while len(picks) < n and any(len(v) > rnd for v in by_q.values()):
            for q in qs:
                lst = by_q.get(q["q"], [])
                if len(lst) > rnd and len(picks) < n:
                    picks.append(lst[rnd])
            rnd += 1

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d")
    lines = [f"📚 <b>이번 주 쉰 블로거 대신 고른 기사</b> ({today})",
             f"<i>{html_escape(', '.join(silent))}</i>"]
    for k, c in enumerate(picks, 1):
        lines.append(f"{k}. {link(c['title'][:110], c['link'])}")
    send_long("\n".join(lines))
