"""미국 ISM 제조업 PMI / 일본 공작기계 수주: 발표 기간에만 확인해 새 발표가 반영되면 10년 차트를 한 번 전송.

Trading Economics 페이지를 실제 브라우저로 열어 '10Y' 차트를 캡처합니다.
새 발표 여부는 페이지 상단 요약문(최신 발표 내용)이 바뀌었는지로 판단합니다.
"""
import sys
import time

from common import (OUT_DIR, browser_page, html_escape, load_config, load_state,
                    notify_error_once, save_state, send_photo, sha)

DESC_SELECTORS = ["#description", ".te-description", "h2"]
RANGE_LABELS = ["10Y", "10년", "10 Y"]
CHART_SELECTORS = ["#trading_chart", ".highcharts-container", "#chart", "#tchart",
                   "[id*='chart']", "[class*='chart']"]


def find_chart_box(page):
    """보이는 차트 후보 중 가장 큰 영역을 찾음."""
    return page.evaluate("""(sels) => {
        let best = null;
        for (const s of sels) for (const el of document.querySelectorAll(s)) {
            const r = el.getBoundingClientRect();
            if (r.width < 400 || r.height < 200 || r.height > 1200) continue;
            const area = r.width * r.height;
            if (!best || area > best.area) best = {x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height, area};
        }
        return best;
    }""", CHART_SELECTORS)


def capture(url, path):
    with browser_page(locale="ko-KR", width=1366, height=1000) as page:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
        title = page.title()
        if any(w in title.lower() for w in ["verification", "access denied", "captcha", "just a moment"]):
            raise RuntimeError(f"Trading Economics 접근 차단됨 (title={title!r})")
        desc = ""
        for s in DESC_SELECTORS:
            el = page.query_selector(s)
            if el and len((el.inner_text() or "").strip()) > 40:
                desc = el.inner_text().strip()
                break
        if not desc:
            raise RuntimeError("요약문을 찾지 못함 (페이지 구조 변경 가능성)")

        clicked = False
        for label in RANGE_LABELS:
            loc = page.get_by_text(label, exact=True)
            if loc.count():
                try:
                    loc.first.click(timeout=5000)
                    clicked = True
                    break
                except Exception:  # noqa: BLE001
                    pass
        page.wait_for_timeout(4000)
        box = find_chart_box(page)
        if box:
            pad = 10
            page.screenshot(path=str(path), full_page=True, clip={
                "x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                "width": box["w"] + pad * 2, "height": box["h"] + pad * 2})
        else:
            page.screenshot(path=str(path))
        return desc, clicked


def main():
    force = "--force" in sys.argv
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    cfg = [i for i in load_config()["macro"] if not only or i["key"] == only]
    st = load_state("macro")
    month = time.strftime("%Y-%m")
    failed = False
    for ind in cfg:
        key = ind["key"]
        prev = st.get(key, {})
        # 발표 기간에 여러 번 확인하지만, 한 달에 한 번만 보냄
        if prev.get("month") == month and not force:
            print(f"{key}: 이번 달은 이미 보냄")
            continue
        path = OUT_DIR / f"{key}.png"
        try:
            desc, clicked = capture(ind["url"], path)
        except Exception as e:  # noqa: BLE001
            failed = True
            notify_error_once(ind["name"], e)
            continue
        h = sha(desc)
        if prev and prev.get("hash") == h and not force:
            print(f"{key}: 아직 새 발표가 반영되지 않음")
            continue
        summary = desc if len(desc) < 600 else desc[:600].rsplit(" ", 1)[0] + "…"
        note = "" if clicked else "\n(10년 버튼을 못 눌러 기본 기간으로 캡처됨)"
        send_photo(path, f"📈 <b>{html_escape(ind['name'])}</b> 새 발표\n\n{html_escape(summary)}{note}\n\n{ind['url']}")
        st[key] = {"hash": h, "month": month, "sent_at": time.strftime("%Y-%m-%d %H:%M")}
        save_state("macro", st)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
