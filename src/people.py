"""매일 06:00 — 지난 24시간 동안 관심 인물들이 AI·주식시장에 대해 직접 한 말·글(인터뷰·연설·메모·SNS)만 링크 (없으면 안 보냄).
이미 보낸 자리(같은 인터뷰·같은 메모)는 45일 동안 다시 보내지 않음. 포트폴리오 변동·해석 기사는 뺌."""
import datetime as dt
import json
import re
import sys

from common import html_escape, load_config, load_state, save_state
from news import (ask_claude_json, dedupe, fetch_rss, google_news, link,
                  llm_available, send_long)

SYSTEM = ("너는 투자 리서치 비서다. 뉴스 제목·요약만 보고, 해당 인물 '본인'이 직접 한 말·글을 전하는 기사만 고른다. "
          "애매하면 뺀다. 아무것도 고르지 않는 것이 정답인 날이 많다.")

# 발언이 아닌 기사(포트폴리오 변동·13F·부동산·자선·소송 등)는 처음부터 뺌
NOT_REMARK = re.compile(
    r"13F|\bstake\b|portfolio|holdings?\b|\bposition|\bbought\b|\bbuys?\b|\bsold\b|\bsells?\b|\bdumps?\b|"
    r"\btrims?\b|\badds?\b.*shares|\bshares of\b|\bfund(s)? (sold|bought)|\bduplex|mansion|penthouse|apartment|"
    r"\bhome\b|real estate|property|donat|philanthrop|lawsuit|\bsues?\b|net worth|richest|"
    r"포트폴리오|지분|매수|매도|사들|팔아|처분|담았|부동산|저택|기부", re.I)


def collect(person, hours=26):
    items = []
    for q in person.get("queries", []):
        try:
            items += google_news(q["q"], q.get("lang", "en"), hours)
        except Exception as e:  # noqa: BLE001
            print(f"{person['name']} {q}: {e}", file=sys.stderr)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    for f in person.get("feeds", []):
        try:
            items += [i for i in fetch_rss(f) if not i["date"] or i["date"] >= cutoff]
        except Exception as e:  # noqa: BLE001
            print(f"{person['name']} {f}: {e}", file=sys.stderr)
    return dedupe(items)[:40]


def pick_with_llm(bundle, topic_hint, already):
    """API 키가 있을 때: 본인 발언 기사만 고르고, 이미 보낸 발언과 같은 내용은 뺌 (요약은 보내지 않음)."""
    payload = [{"person": name, "articles": [
        {"id": k, "title": i["title"], "source": i["source"], "snippet": i["desc"][:200]}
        for k, i in enumerate(items)]} for name, items in bundle.items() if items]
    prompt = (f"인물별 지난 24시간 뉴스 목록이다. 각 인물 '본인'이 '{topic_hint}'에 대해 직접 한 말·글을 전하는 기사만 골라라.\n"
              "포함: 본인이 나온 인터뷰·팟캐스트·방송·컨퍼런스 연설, 본인이 쓴 서한·메모·칼럼·SNS 글, "
              "또는 그런 자리의 발언을 따옴표로 그대로 옮긴 기사. 제목·요약에 그 자리(인터뷰, 연설, 메모, 글 등)와 발언이 드러나야 한다.\n"
              "제외(하나라도 해당하면 뺀다):\n"
              " - 포트폴리오·13F·매수·매도·지분 변동 기사 (본인이 직접 설명한 인터뷰가 아니면)\n"
              " - 남이 그 사람의 생각·전략을 해석·분석·추측한 기사 ('~가 보는 시장', '~를 따라 할까', '~가 팔았다, 누가 맞나' 류)\n"
              " - 예전 발언을 다시 모아 소개하는 기사, 명언 모음\n"
              " - 부동산·자선·개인사·소송·재산, 그 사람 회사(시타델·버크셔·테슬라 등)의 일반 뉴스, 이름만 나오는 일반 기사\n"
              " - 동명이인\n"
              "같은 자리(같은 인터뷰·같은 메모·같은 글)에서 나온 발언은 기사가 여러 개여도 하나의 occasion으로 묶고 그중 원문에 가장 가까운 기사 1개만.\n"
              "아래 '이미 보낸 자리'와 같은 자리를 다룬 기사는 제목이 달라도 제외. 같은 사람의 새 인터뷰·새 글만 포함.\n"
              f"[이미 보낸 자리]\n{json.dumps(already, ensure_ascii=False)}\n\n"
              '출력: [{"person": 이름, "ids": [기사 id], "occasion": "어떤 자리인지 한 줄 (예: 11월 TomorrowX 인터뷰, 11월 오크트리 메모 \'제목\')", '
              '"remark": "핵심 발언 한 줄"}] — 해당하는 게 없으면 빈 배열 [].\n\n'
              + json.dumps(payload, ensure_ascii=False))
    return ask_claude_json(prompt, SYSTEM, max_tokens=1500)


def _key(title):
    return re.sub(r"\W+", "", title.lower())[:60]


def main():
    cfg = load_config()["people"]
    st = load_state("people")
    cutoff = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    sent = [x for x in st.get("sent", []) if x.get("date", "") >= cutoff]  # 최근 45일 보낸 발언·기사
    sent_titles = {x.get("key") for x in sent}
    bundle = {}
    for p in cfg["list"]:  # 이미 보낸 기사는 처음부터 제외
        bundle[p["name"]] = [i for i in collect(p) if _key(i["title"]) not in sent_titles
                             and not NOT_REMARK.search(i["title"])]
    if sum(len(v) for v in bundle.values()) == 0:
        print("새 기사 없음")
        return

    chosen, remarks = None, {}
    if llm_available():
        try:
            seen_occ, already = set(), []
            for x in sent:
                k = (x["person"], x.get("occasion") or x.get("remark", ""))
                if k[1] and k not in seen_occ:
                    seen_occ.add(k)
                    already.append({"person": x["person"], "occasion": x.get("occasion", ""),
                                    "remark": x.get("remark", ""), "date": x["date"]})
            chosen, today_occ = {}, set()
            for r in pick_with_llm(bundle, cfg["topic_hint"], already):
                name = r.get("person")
                if name in bundle:
                    picks = [bundle[name][i] for i in r.get("ids", []) if isinstance(i, int) and 0 <= i < len(bundle[name])][:1]
                    occ = (name, r.get("occasion", ""))
                    if picks and occ[1] and occ in today_occ:
                        continue  # 같은 자리는 하루에 하나만
                    today_occ.add(occ)
                    if picks:
                        chosen.setdefault(name, []).extend(picks)
                        remarks.setdefault(name, []).append((r.get("occasion", ""), r.get("remark", ""), picks))
        except Exception as e:  # noqa: BLE001
            print(f"AI 선별 실패, 키워드로 대신 고름: {e}", file=sys.stderr)
            chosen = None
    if chosen is None:
        print("AI 선별을 못 해서 오늘은 보내지 않음 (해석 기사가 섞일 수 있어서)")
        return
    if False:
        # 영어 단어는 단어 단위로 비교 ('ai'가 'said' 안에서 걸리지 않도록)
        pats = [re.compile(r"\b" + re.escape(k.lower()) + r"\b") if k.isascii() else re.compile(re.escape(k))
                for k in cfg["fallback_keywords"]]
        chosen = {name: [i for i in items if any(p.search(i["title"].lower()) for p in pats)][:3]
                  for name, items in bundle.items()}

    blocks = [f"<b>{html_escape(name)}</b>\n" + "\n".join(
        f"  · {link(i['title'][:110], i['link'])} ({html_escape(i['source'])})" for i in items)
        for name, items in chosen.items() if items]
    if not blocks:
        print("관련 발언 없음 → 메시지 안 보냄")
        return
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d")
    send_long(f"🗣 <b>투자 거물 발언</b> ({today}, 지난 24시간)\n\n" + "\n\n".join(blocks))
    for name, groups in remarks.items():
        for occasion, remark, items in groups:
            for i in items:
                sent.append({"date": dt.date.today().isoformat(), "person": name, "key": _key(i["title"]),
                             "occasion": occasion, "remark": remark})
    st["sent"] = sent
    save_state("people", st)


if __name__ == "__main__":
    main()
