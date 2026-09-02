"""
aa_oberon_extractor.py
======================
Adobe Analytics Workspace 프로젝트의 각 Visualization에서
Oberon JSON Request를 추출하여 로컬에 저장하는 자동화 스크립트.

동작 흐름
---------
 1. 브라우저(비 headless)를 열고 Workspace 프로젝트 URL로 이동
 2. 사용자가 수동으로 SSO 로그인 완료 → 터미널에서 Enter
 3. Analytics iframe 내부에서 `adobeTools.debug.includeOberonXml = true`
    를 실행하여 디버그 모드(Bug 아이콘) 활성화
 4. 접혀 있는 모든 Panel을 펼침
 5. Panel → Visualization 순회하며 Bug 아이콘 클릭
 6. 팝오버에 나타나는 "... JSON Request" 링크 클릭 → 새 창의 JSON 텍스트 추출
 7. `output_json/{패널명}__{컴포넌트명}__reqN.json` 으로 저장

사전 준비
---------
    pip install playwright
    playwright install chromium

실행
----
    python aa_oberon_extractor.py "https://experience.adobe.com/#/@yourorg/analytics/..."

주의
----
* Adobe가 Workspace DOM을 수시로 바꾸기 때문에 SELECTORS 딕셔너리의
  셀렉터는 릴리즈에 따라 조정이 필요할 수 있음. 스크립트가 요소를 못 찾으면
  DevTools로 실제 DOM을 확인하고 SELECTORS 값만 고치면 됨.
* 로그인 세션은 USER_DATA_DIR(영구 프로필)에 저장되므로,
  두 번째 실행부터는 로그인 단계가 대부분 생략됨.
"""

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    Frame,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("output_json")          # 결과 저장 폴더
USER_DATA_DIR = Path(".pw_profile")       # 로그인 세션 유지용 크롬 프로필
DEFAULT_TIMEOUT_MS = 30_000               # 요소 대기 기본 타임아웃 (여유 있게)
PAGE_LOAD_EXTRA_WAIT_S = 8                # Workspace 초기 렌더링 추가 대기
AFTER_PANEL_EXPAND_WAIT_S = 3             # 패널 펼친 뒤 시각화 로딩 대기

# Workspace DOM 셀렉터 모음.
# 여러 후보를 리스트로 두고 순서대로 시도한다 (Adobe 릴리즈별 DOM 변화 대응).
SELECTORS = {
    # 프로젝트 캔버스 (로딩 완료 판정용)
    "canvas": [
        "[data-testid='project-canvas']",
        ".project-canvas",
        ".an-canvas",
    ],
    # 패널 컨테이너
    "panel": [
        "[data-testid='panel']",
        ".panel-body-wrapper",
        "div[class*='panel'][class*='wrapper']",
    ],
    # 패널 헤더의 접기/펼치기 토글 (aria-expanded 로 상태 판별)
    "panel_toggle": [
        "button[aria-expanded]",
        "[data-testid='panel-collapse-toggle']",
    ],
    # 패널 제목
    "panel_title": [
        "[data-testid='panel-title']",
        ".panel-header input",
        ".panel-header [class*='title']",
    ],
    # 시각화(비주얼라이제이션) 컨테이너
    "viz": [
        "[data-testid='visualization']",
        ".vis-container",
        "div[class*='freeform'],div[class*='visualization']",
    ],
    # 시각화 제목
    "viz_title": [
        "[data-testid='viz-title']",
        ".vis-header input",
        ".vis-header [class*='title']",
    ],
    # 디버그(Bug) 아이콘 — adobeTools 디버그 모드 활성화 후에만 나타남
    "bug_icon": [
        "[title*='debug' i]",
        "[class*='debug']",
        "button:has(svg[class*='bug'])",
    ],
    # Bug 아이콘 클릭 시 뜨는 팝오버 안의 'JSON Request' 링크들
    "json_request_link": [
        "a:has-text('JSON Request')",
        "span:has-text('JSON Request')",
        "[class*='debug'] a",
    ],
}


# ─────────────────────────────────────────────────────────────
# 유틸리티
# ─────────────────────────────────────────────────────────────
def sanitize(name: str, fallback: str) -> str:
    """패널/컴포넌트 이름을 파일명으로 쓸 수 있게 정리."""
    name = (name or "").strip() or fallback
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", name)
    return name[:80]  # 파일명 길이 제한


def first_locator(scope, keys: list[str]):
    """
    후보 셀렉터 리스트를 순서대로 시도해서
    실제로 존재하는 첫 번째 locator를 반환. 없으면 None.
    scope: Frame 또는 Locator
    """
    for sel in keys:
        loc = scope.locator(sel)
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def find_analytics_frame(page: Page) -> Frame:
    """
    experience.adobe.com 통합 셸은 실제 Analytics 앱을 iframe으로 띄운다.
    analytics.adobe.com 을 포함하는 frame을 찾아 반환하고,
    (구형 URL 등으로) iframe이 없으면 main frame을 그대로 사용.
    """
    deadline = time.time() + 60
    while time.time() < deadline:
        for f in page.frames:
            if "analytics" in (f.url or "") and f is not page.main_frame:
                return f
        time.sleep(1)
    return page.main_frame


def enable_oberon_debugger(frame: Frame) -> bool:
    """
    Analytics frame 컨텍스트에서 Oberon 디버거 활성화.
    성공하면 각 시각화 헤더에 Bug 아이콘이 나타난다.
    """
    try:
        frame.evaluate(
            """() => {
                if (window.adobeTools && window.adobeTools.debug) {
                    window.adobeTools.debug.includeOberonXml = true;
                    return true;
                }
                return false;
            }"""
        )
        return True
    except Exception as e:
        print(f"  [!] 디버거 활성화 실패: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# 핵심 로직
# ─────────────────────────────────────────────────────────────
def expand_all_panels(frame: Frame) -> None:
    """aria-expanded='false' 인 패널 토글을 모두 클릭해 펼친다."""
    print("[*] 접힌 패널 펼치는 중...")
    expanded = 0
    for sel in SELECTORS["panel_toggle"]:
        toggles = frame.locator(f"{sel}[aria-expanded='false']")
        try:
            count = toggles.count()
        except Exception:
            continue
        for i in range(count):
            try:
                toggles.nth(i).click(timeout=5_000)
                expanded += 1
                time.sleep(0.5)  # 펼침 애니메이션 대기
            except Exception:
                pass
    if expanded:
        print(f"    → 패널 {expanded}개 펼침. 시각화 로딩 대기 {AFTER_PANEL_EXPAND_WAIT_S}s")
        time.sleep(AFTER_PANEL_EXPAND_WAIT_S)
    else:
        print("    → 접힌 패널 없음 (또는 토글 셀렉터 미일치)")


def extract_json_from_popup(popup: Page) -> str | None:
    """
    JSON Request 링크 클릭 시 열리는 새 창에서 JSON 본문 추출.
    보통 <pre> 태그 또는 body 전체가 JSON 텍스트다.
    """
    try:
        popup.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        # <pre> 우선, 없으면 body 텍스트
        pre = popup.locator("pre")
        raw = pre.first.inner_text() if pre.count() > 0 else popup.inner_text("body")
        raw = raw.strip()

        # 텍스트에서 가장 바깥 JSON 오브젝트만 잘라내기
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return None
        candidate = raw[start : end + 1]
        json.loads(candidate)  # 유효성 검증 (실패 시 예외 → None)
        return candidate
    except Exception:
        return None
    finally:
        try:
            popup.close()
        except Exception:
            pass


def process_visualization(frame: Frame, page: Page, viz, panel_name: str, viz_idx: int) -> int:
    """
    시각화 하나 처리: 호버 → Bug 아이콘 클릭 → JSON Request 링크 순회 → 저장.
    반환값: 저장한 파일 개수.
    """
    saved = 0

    # 컴포넌트 이름 추출 (실패 시 인덱스 기반 이름)
    title_loc = first_locator(viz, SELECTORS["viz_title"])
    viz_name = ""
    if title_loc:
        try:
            el = title_loc.first
            viz_name = el.input_value() if el.evaluate("e => e.tagName") == "INPUT" else el.inner_text()
        except Exception:
            pass
    viz_name = sanitize(viz_name, f"viz{viz_idx:02d}")

    # 1) 헤더 노출을 위해 스크롤 & 호버
    viz.scroll_into_view_if_needed(timeout=10_000)
    viz.hover(timeout=10_000)
    time.sleep(0.8)

    # 2) Bug 아이콘 찾기 (시각화 내부에서만 탐색)
    bug = first_locator(viz, SELECTORS["bug_icon"])
    if not bug:
        print(f"    [-] '{viz_name}': Bug 아이콘 없음 (디버그 미지원 컴포넌트일 수 있음)")
        return 0
    bug.first.click(timeout=10_000)
    time.sleep(1.0)  # 팝오버 렌더 대기

    # 3) 팝오버 안의 JSON Request 링크 수집 (frame 전역에서 탐색 — 팝오버는 viz 밖에 붙음)
    links = first_locator(frame, SELECTORS["json_request_link"])
    if not links:
        print(f"    [-] '{viz_name}': JSON Request 링크 없음")
        page.keyboard.press("Escape")
        return 0

    n_links = links.count()
    for li in range(n_links):
        try:
            # 링크 클릭 → 새 창(popup) 열림을 기다렸다가 JSON 추출
            with page.context.expect_page(timeout=DEFAULT_TIMEOUT_MS) as pop_info:
                links.nth(li).click(timeout=10_000)
            data = extract_json_from_popup(pop_info.value)
            if data:
                fname = OUTPUT_DIR / f"{panel_name}__{viz_name}__req{li + 1}.json"
                fname.write_text(data, encoding="utf-8")
                print(f"    [+] 저장: {fname.name}")
                saved += 1
            else:
                print(f"    [-] '{viz_name}' req{li + 1}: JSON 파싱 실패")
        except PWTimeout:
            print(f"    [-] '{viz_name}' req{li + 1}: 팝업 대기 타임아웃")
        except Exception as e:
            print(f"    [-] '{viz_name}' req{li + 1}: {e}")

    # 4) 팝오버 닫고 다음 컴포넌트로
    page.keyboard.press("Escape")
    time.sleep(0.5)
    return saved


def run(project_url: str) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        # 영구 프로필 사용 → SSO 로그인 세션 재사용 가능
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,               # 로그인/디버그 확인을 위해 반드시 창 표시
            viewport={"width": 1680, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        # ── 1. 프로젝트 이동 & 수동 로그인 ──────────────────────
        print(f"[*] 프로젝트로 이동: {project_url}")
        page.goto(project_url, wait_until="domcontentloaded", timeout=120_000)
        input("\n>>> 브라우저에서 로그인을 완료하고 프로젝트가 화면에 보이면 Enter를 누르세요... ")

        # ── 2. Analytics iframe 확보 & 캔버스 로딩 대기 ─────────
        frame = find_analytics_frame(page)
        print(f"[*] Analytics frame: {frame.url[:80]}...")
        canvas = first_locator(frame, SELECTORS["canvas"])
        if canvas:
            canvas.first.wait_for(state="visible", timeout=60_000)
        time.sleep(PAGE_LOAD_EXTRA_WAIT_S)  # 시각화 렌더링 여유 대기

        # ── 3. Oberon 디버거 활성화 ────────────────────────────
        print("[*] Oberon 디버거 활성화 (adobeTools.debug.includeOberonXml = true)")
        enable_oberon_debugger(frame)
        time.sleep(2)

        # ── 4. 패널 전체 펼치기 ────────────────────────────────
        expand_all_panels(frame)

        # ── 5. 패널 → 시각화 순회 ──────────────────────────────
        panels = first_locator(frame, SELECTORS["panel"])
        if not panels:
            print("[!] 패널을 찾지 못했습니다. SELECTORS['panel'] 을 실제 DOM에 맞게 수정하세요.")
            context.close()
            return

        total_saved = 0
        n_panels = panels.count()
        print(f"[*] 패널 {n_panels}개 발견\n")

        for pi in range(n_panels):
            panel = panels.nth(pi)

            # 패널 이름
            p_title = first_locator(panel, SELECTORS["panel_title"])
            panel_name = ""
            if p_title:
                try:
                    el = p_title.first
                    panel_name = el.input_value() if el.evaluate("e => e.tagName") == "INPUT" else el.inner_text()
                except Exception:
                    pass
            panel_name = sanitize(panel_name, f"panel{pi + 1:02d}")
            print(f"[Panel {pi + 1}/{n_panels}] {panel_name}")

            # 패널 내 시각화 순회 — 개별 실패가 전체를 멈추지 않도록 try-except
            vizzes = first_locator(panel, SELECTORS["viz"])
            if not vizzes:
                print("    (시각화 없음)")
                continue
            for vi in range(vizzes.count()):
                try:
                    total_saved += process_visualization(
                        frame, page, vizzes.nth(vi), panel_name, vi + 1
                    )
                except Exception as e:
                    print(f"    [-] viz{vi + 1:02d} 처리 중 예외 → 건너뜀: {e}")
                    # 열려 있을지 모르는 팝오버 정리
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass

        print(f"\n[완료] 총 {total_saved}개 JSON 저장 → {OUTPUT_DIR.resolve()}")
        context.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python aa_oberon_extractor.py <Workspace 프로젝트 URL>")
        sys.exit(1)
    run(sys.argv[1])
