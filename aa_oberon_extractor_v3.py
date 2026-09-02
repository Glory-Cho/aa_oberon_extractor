"""
aa_oberon_extractor_v2.py  (최종본 — probe v3/v4로 확정한 실제 DOM 셀렉터 사용)
==============================================================================
실행 중인 실제 Chrome(CDP)에 붙어, Workspace 프로젝트의 모든 패널을
하나씩 펼치고 각 Visualization의 Oberon 디버그 버튼을 눌러
JSON Request를 output_json/ 에 저장한다.

사전 준비
---------
1) Chrome 전부 종료 후 디버그 포트로 실행:
   "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
       --remote-debugging-port=9222 --user-data-dir=C:\\chrome-debug-profile
2) 그 Chrome에서 SSO 로그인 → Workspace 프로젝트 열기 → Enable Debugger 켜기
   (시각화 헤더에 벌레 아이콘이 보여야 함)
3) python aa_oberon_extractor_v2.py

동작 설계
---------
* 패널 단위 순차 처리: 접힌 패널을 펼침 → 내부 viz 추출 → 원래 접혀 있었다면
  다시 접음 → 다음 패널. (전체 펼침 방식 대비 메모리/렌더링 부하 일정)
* 이름 매핑: aria-label 에서 직접 추출
    - 패널:  section.an-panel[aria-label="Panel: <이름>"]
    - 시각화: section.an-sub-panel[aria-label="Visualization: <이름>"]
* 디버그 버튼: button.oberon-xml-debug (디버거 활성화 시 항상 DOM에 존재)
* 팝오버의 링크 클릭 → 새 창의 JSON 파싱·저장
* 자가 진단: 팝오버에서 링크를 못 찾으면 해당 팝오버 HTML을
  output_json/_debug/ 에 덤프하고 계속 진행 (다음 세션에서 셀렉터 보정용)
"""

import json
import re
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# ── 설정 ─────────────────────────────────────────────────────
CDP_URL = "http://localhost:9222"
OUTPUT_DIR = Path("output_json")
DEBUG_DIR = OUTPUT_DIR / "_debug"
POPUP_TIMEOUT_MS = 20_000        # JSON 새 창 대기
PANEL_RENDER_WAIT_S = 30         # 패널 펼침 후 viz 렌더 최대 대기
RESTORE_COLLAPSED = True         # 원래 접혀 있던 패널은 처리 후 다시 접기

# ── probe로 확정한 셀렉터 (추측 아님) ────────────────────────
SEL = {
    "panel": "section.an-panel",
    "panel_toggle": ".panel-collapse-button",
    "panel_expanded_class": "has-visible-subpanels",
    "viz": "section.an-sub-panel",
    "debug_btn": "button.oberon-xml-debug",
    # 팝오버/메뉴 오버레이 컨테이너 후보 (menu 역할 포함)
    "overlay": (
        "[role='dialog'], [role='menu'], [role='listbox'], .spectrum-Popover, "
        ".tether-element, [class*='oberon'], [class*='debug']"
    ),
}

# ── 팝오버 2단계 메뉴 설정 ───────────────────────────────────
# 실제 흐름: 디버그 버튼 → 'Freeform Table' 클릭 → 시간대 목록 → 시간대 클릭 → 새 창
FIRST_LEVEL_TEXT = "Freeform"   # 1단계 메뉴 텍스트 (부분 일치, 대소문자 무시)
TIMESTAMP_RE = r"\d{1,2}:\d{2}"  # 2단계 시간대 항목 판별 패턴
TIMESTAMP_PICK = "first"         # 'first'=목록 첫 항목 / 'last'=마지막 항목
                                 # → 최신이 목록 어느 쪽인지에 따라 바꿀 것


def sanitize(name: str, fallback: str) -> str:
    name = (name or "").strip() or fallback
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", name)
    return name[:70]


def aria_name(locator, prefix: str, fallback: str) -> str:
    """aria-label 'Panel: X' / 'Visualization: X' 에서 X 추출."""
    try:
        label = locator.get_attribute("aria-label") or ""
        if label.startswith(prefix):
            return sanitize(label[len(prefix):], fallback)
        return sanitize(label, fallback)
    except Exception:
        return fallback


def find_canvas_frame(browser):
    """adobeTools 가 존재하는 frame = Analytics 캔버스."""
    for ctx in browser.contexts:
        for pg in ctx.pages:
            for f in pg.frames:
                try:
                    if f.evaluate("() => !!(window.adobeTools && window.adobeTools.debug)"):
                        return f
                except Exception:
                    continue
    return None


def dump_popover_debug(frame, tag: str) -> None:
    """팝오버에서 링크를 못 찾았을 때 구조를 파일로 덤프 (다음 세션 보정용)."""
    try:
        html = frame.evaluate(
            """(sel) => {
                const parts = [];
                for (const el of document.querySelectorAll(sel)) {
                    if (!el.getClientRects().length) continue;   // 비가시 요소 제외
                    parts.push(el.outerHTML.slice(0, 20000));
                }
                // 팝오버가 body 끝에 동적으로 붙는 경우 대비: body 마지막 5개 자식도 덤프
                const tail = [...document.body.children].slice(-5)
                    .map(e => e.outerHTML.slice(0, 8000));
                return parts.join('\\n\\n===VISIBLE-CANDIDATE===\\n\\n')
                    + '\\n\\n===BODY-TAIL===\\n\\n' + tail.join('\\n---\\n');
            }""",
            SEL["overlay"],
        )
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"popover_{tag}.html").write_text(html, encoding="utf-8")
        print(f"      [debug] 팝오버 구조 덤프 → _debug/popover_{tag}.html")
    except Exception as e:
        print(f"      [debug] 덤프 실패: {e}")


def _scan_json_objects(text: str) -> list[str]:
    """
    텍스트에서 최상위 JSON 오브젝트들을 추출.
    문자열 내부의 { } 를 오인하지 않도록 따옴표/이스케이프를 인식하는
    brace 매칭 스캐너. (Oberon 창에는 Request와 Response가 함께 있어
    단순 find('{')~rfind('}') 방식은 두 오브젝트가 합쳐져 파싱 실패함)
    """
    objs, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "{":
            depth, j, in_str, esc = 0, i, False, False
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            objs.append(text[i:j + 1])
                            i = j
                            break
                j += 1
        i += 1
    valid = []
    for o in objs:
        try:
            json.loads(o)
            valid.append(o)
        except Exception:
            pass
    return valid


def extract_json_from_popup(popup) -> str | None:
    """Oberon 새 창에서 'JSON Request' 본문 추출·검증."""
    try:
        popup.wait_for_load_state("domcontentloaded", timeout=POPUP_TIMEOUT_MS)
        raw = ""
        for _ in range(10):  # 동적 렌더 지연 대비 폴링 (최대 ~7초)
            time.sleep(0.7)
            try:
                raw = popup.inner_text("body").strip()
            except Exception:
                raw = ""
            if len(raw) > 50:
                break
        if not raw:
            return None
        # 'JSON Request' 마커 뒤의 첫 오브젝트를 우선 (Response 오인 방지)
        m = re.search(r"JSON\s*Request", raw, re.I)
        scope = raw[m.end():] if m else raw
        objs = _scan_json_objects(scope)
        if not objs and m:          # 마커 뒤 실패 시 전체에서 재시도
            objs = _scan_json_objects(raw)
        return objs[0] if objs else None
    except Exception:
        return None
    finally:
        try:
            popup.close()
        except Exception:
            pass


def _click_first_visible(loc) -> bool:
    """locator 매칭 중 화면에 보이는 첫 요소 클릭. 성공 여부 반환."""
    try:
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            if el.is_visible():
                el.click(timeout=8_000)
                return True
    except Exception:
        pass
    return False


def process_viz(frame, page, viz, panel_name: str, vi: int) -> int:
    """
    viz 하나 처리. 실제 UI 흐름:
      디버그 버튼 → [1단계] 'Freeform Table' 메뉴 → [2단계] 시간대 목록
      → 시간대 클릭 → Oberon 새 창 → JSON Request 추출
    반환: 저장 수 (0 또는 1).
    """
    viz_name = aria_name(viz, "Visualization: ", f"viz{vi:02d}")
    tag = f"{sanitize(panel_name, 'p')}_{viz_name}"

    dbg = viz.locator(SEL["debug_btn"])
    if dbg.count() == 0:
        print(f"    [-] {viz_name}: 디버그 버튼 없음 (Text 등 비-Oberon 컴포넌트) → 건너뜀")
        return 0

    viz.scroll_into_view_if_needed(timeout=10_000)
    dbg.first.click(timeout=10_000)
    time.sleep(1.2)

    overlay = frame.locator(SEL["overlay"])

    # ── 1단계: 'Freeform Table' 메뉴 항목 클릭 ──
    lvl1 = overlay.get_by_text(re.compile(FIRST_LEVEL_TEXT, re.I))
    if lvl1.count() > 0:
        if not _click_first_visible(lvl1):
            print(f"    [-] {viz_name}: 1단계 메뉴 클릭 실패 → 덤프")
            dump_popover_debug(frame, f"{tag}_lvl1")
            page.keyboard.press("Escape")
            return 0
        time.sleep(1.0)
    # (1단계 항목이 없으면 팝오버가 곧바로 시간대 목록인 경우로 보고 진행)

    # ── 2단계: 시간대 목록에서 최신 항목 클릭 ──
    ts = frame.locator(SEL["overlay"]).get_by_text(re.compile(TIMESTAMP_RE))
    n = ts.count()
    if n == 0:
        print(f"    [-] {viz_name}: 시간대 항목 0개 → 구조 덤프 후 건너뜀")
        dump_popover_debug(frame, f"{tag}_lvl2")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        return 0

    idx = 0 if TIMESTAMP_PICK == "first" else n - 1
    try:
        ts_label = sanitize(ts.nth(idx).inner_text()[:30], "latest")
    except Exception:
        ts_label = "latest"

    saved = 0
    try:
        with page.context.expect_page(timeout=POPUP_TIMEOUT_MS) as pop_info:
            ts.nth(idx).click(timeout=10_000)
        data = extract_json_from_popup(pop_info.value)
        if data:
            fname = OUTPUT_DIR / f"{panel_name}__{viz_name}__{ts_label}.json"
            fname.write_text(data, encoding="utf-8")
            print(f"    [+] 저장: {fname.name}")
            saved = 1
        else:
            print(f"    [-] {viz_name}: 새 창에서 JSON Request 추출 실패")
    except PWTimeout:
        print(f"    [-] {viz_name}: 새 창 대기 타임아웃 → 시간대 클릭이 새 창을 안 열었거나 "
              f"팝업 차단. (chrome://settings/content/popups 에서 adobe 도메인 허용)")
        dump_popover_debug(frame, f"{tag}_ts_noclick")
    except Exception as e:
        print(f"    [-] {viz_name}: {e}")

    page.keyboard.press("Escape")  # 중첩 메뉴 닫기
    page.keyboard.press("Escape")
    time.sleep(0.4)
    return saved


def wait_for_panel_render(panel) -> None:
    """패널 펼침 후 viz가 나타날 때까지 폴링."""
    deadline = time.time() + PANEL_RENDER_WAIT_S
    while time.time() < deadline:
        try:
            if panel.locator(SEL["viz"]).count() > 0:
                time.sleep(2.5)  # 데이터 로딩 여유
                return
        except Exception:
            pass
        time.sleep(1)


def run() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    manifest = {"panels": [], "total_saved": 0, "started": time.strftime("%Y-%m-%d %H:%M:%S")}

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"[!] Chrome CDP 접속 실패: {e}")
            print("    → chrome.exe --remote-debugging-port=9222 --user-data-dir=... 확인")
            return

        frame = find_canvas_frame(browser)
        if not frame:
            print("[!] Analytics 캔버스 frame(adobeTools)을 찾지 못함. 프로젝트가 열려 있는지 확인.")
            return
        page = frame.page
        print(f"[*] 캔버스 frame 연결: {frame.url[:80]}")

        # 디버그 버튼 존재 확인 (Enable Debugger 여부 검증)
        if frame.locator(SEL["debug_btn"]).count() == 0:
            print("[!] 디버그 버튼(oberon-xml-debug)이 하나도 없음.")
            print("    → Enable Debugger를 켜고 다시 실행하세요 (새로고침 시 꺼질 수 있음).")
            return

        panels = frame.locator(SEL["panel"])
        n_panels = panels.count()
        print(f"[*] 패널 {n_panels}개 발견\n")

        total = 0
        for pi in range(n_panels):
            panel = panels.nth(pi)
            panel_name = aria_name(panel, "Panel: ", f"panel{pi+1:02d}")
            print(f"[Panel {pi+1}/{n_panels}] {panel_name}")
            p_entry = {"name": panel_name, "viz_saved": 0, "errors": 0}

            try:
                # ── 펼침 상태 확인, 접혀 있으면 펼침 ──
                cls = panel.get_attribute("class") or ""
                was_collapsed = SEL["panel_expanded_class"] not in cls
                if was_collapsed:
                    panel.locator(SEL["panel_toggle"]).first.click(timeout=10_000)
                    wait_for_panel_render(panel)

                # ── viz 순회 ──
                vizzes = panel.locator(SEL["viz"])
                nv = vizzes.count()
                if nv == 0:
                    print("    (시각화 없음 — readme/빈 패널)")
                for vi in range(nv):
                    try:
                        got = process_viz(frame, page, vizzes.nth(vi), panel_name, vi + 1)
                        total += got
                        p_entry["viz_saved"] += got
                    except Exception as e:
                        p_entry["errors"] += 1
                        print(f"    [-] viz{vi+1:02d} 예외 → 건너뜀: {e}")
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass

                # ── 원래 접혀 있던 패널은 다시 접기 (부하 관리) ──
                if was_collapsed and RESTORE_COLLAPSED:
                    try:
                        panel.locator(SEL["panel_toggle"]).first.click(timeout=10_000)
                        time.sleep(0.8)
                    except Exception:
                        pass

            except Exception as e:
                p_entry["errors"] += 1
                print(f"  [-] 패널 처리 예외 → 다음 패널로: {e}")

            manifest["panels"].append(p_entry)

        manifest["total_saved"] = total
        manifest["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (OUTPUT_DIR / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[완료] 총 {total}개 JSON 저장 → {OUTPUT_DIR.resolve()}")
        print("       요약: output_json/_manifest.json")


if __name__ == "__main__":
    run()
