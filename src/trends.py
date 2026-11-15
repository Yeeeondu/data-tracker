"""구글 트렌드 비교군을 매주 캡처해 텔레그램 앨범으로 전송."""
import sys
import time
import urllib.parse

from common import (OUT_DIR, browser_page, html_escape, load_config,
                    notify_error_once, send_media_group, send_message)

CHART_SELECTORS = ["widget[type='fe_line_chart']", ".fe-line-chart",
                   "[class*='fe-line-chart']", "line-chart-directive"]


def kw_id(k):
    return k["id"] if isinstance(k, dict) else k


def kw_label(k):
    return k["label"] if isinstance(k, dict) else k


def explore_url(group, timeframe, hl):
    kws = [kw_id(k) for k in group["keywords"]]
    geo = group.get("geo") or ""
    date = group.get("timeframe", timeframe)
    if isinstance(geo, list):  # 국가별 비교: 기간도 키워드 수만큼 반복해야 함
        date = ",".join([date] * len(kws))
        geo = ",".join(geo)
    q = {"date": date, "q": ",".join(kws), "hl": hl}
    if geo:
        q["geo"] = geo
    return "https://trends.google.com/trends/explore?" + urllib.parse.urlencode(
        q, safe=",/", quote_via=urllib.parse.quote)


def capture_group(page, url, path, tries=3):
    for attempt in range(tries):
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(7000 + attempt * 5000)
        body = page.inner_text("body")[:3000].lower()
        if "oops" in body or "429" in body or "문제가 발생" in body:
            time.sleep(20 * (attempt + 1))
            continue
        for s in CHART_SELECTORS:
            loc = page.locator(s)
            if loc.count():
                loc.first.scroll_into_view_if_needed()
                page.wait_for_timeout(1500)
                loc.first.screenshot(path=str(path))
                return True
        time.sleep(10)
    return False


def main():
    cfg = load_config()["trends"]
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    groups = cfg["groups"]
    if only:
        groups = [g for g in groups if only in g["title"]]
    shots, failed = [], []
    with browser_page(locale="ko-KR", width=1280, height=1100) as page:
        page.goto("https://trends.google.com/trends/?hl=ko", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        for i, g in enumerate(groups):
            url = explore_url(g, cfg["timeframe"], cfg["hl"])
            path = OUT_DIR / f"trends_{i:02d}.png"
            ok = False
            try:
                ok = capture_group(page, url, path)
            except Exception as e:  # noqa: BLE001
                print(f"{g['title']}: {e}", file=sys.stderr)
            if ok:
                shots.append((path, f"<b>{html_escape(g['title'])}</b>\n{html_escape(' vs '.join(kw_label(k) for k in g['keywords']))}"
                                    f"\n<a href=\"{html_escape(url)}\">트렌드에서 열기</a>"))
            else:
                failed.append((g["title"], url))
            time.sleep(8)  # 구글 요청 제한 회피

    today = time.strftime("%Y-%m-%d")
    if shots:
        # 모든 그래프를 하나의 앨범으로 (텔레그램 앨범은 최대 10장이라 넘으면 나눠 보냄)
        head = f"🔎 <b>주간 구글 트렌드</b> ({today})\n\n"
        first_path, first_cap = shots[0]
        shots[0] = (first_path, head + first_cap)
        for k in range(0, len(shots), 10):
            send_media_group(shots[k:k + 10])
    if failed:
        lines = "\n".join(f"• <a href=\"{html_escape(u)}\">{html_escape(t)}</a>" for t, u in failed)
        send_message(f"⚠️ 캡처 실패한 비교군 (링크로 직접 확인):\n{lines}", silent=True)
    if not shots:
        notify_error_once("구글 트렌드", "모든 비교군 캡처 실패 (구글 요청 제한 가능성)")
        sys.exit(1)


if __name__ == "__main__":
    main()
