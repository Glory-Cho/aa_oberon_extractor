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
SAVE_DIR = OUTPUT_DIR            # run()에서 'output_json/<워크스페이스명>/' 으로 재설정
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

# ── 대형 워크스페이스 대응 설정 ──────────────────────────────
SKIP_EXISTING = True    # 같은 패널__viz 파일이 이미 있으면 건너뜀 (중단 후 이어받기)
VIZ_RETRIES = 1         # viz 처리 예외 시 재시도 횟수
TS_OPEN_TRIES = 3       # 시간대 목록이 비어 있을 때 디버그 메뉴 재오픈 횟수
TS_RETRY_WAIT_S = 4     # 재오픈 전 대기 (막 펼친 패널은 요청 발사까지 시간 걸림)
PANEL_BASE_WAIT_S = 2   # 패널 펼침 후 기본 대기 (요청 '발사'만 기다리면 됨)
PER_VIZ_WAIT_S = 0.5    # viz 1개당 추가 대기
PANEL_MAX_WAIT_S = 20   # 패널당 대기 상한

# ── 속도 파라미터 (느리면 줄이고, '시간대 0개' 실패가 늘면 다시 올릴 것) ──
MENU_OPEN_WAIT_S = 0.6    # 디버그 버튼 클릭 → 팝오버 렌더 대기
LVL1_WAIT_S = 0.5         # 'Freeform Table' 클릭 → 시간대 목록 렌더 대기
EXTRACT_POLLS = 12        # Oberon 뷰 추출 재시도 횟수
EXTRACT_POLL_S = 0.5      # 추출 재시도 간격
VIZ_EXPAND_WAIT_S = 1.2   # 접힌 viz 펼침 후 대기
CLOSE_WAIT_S = 0.4        # Cancel 클릭 후 대기

# 클립보드 폴백: 포커스를 뺏고 시스템 클립보드를 덮어쓰므로
# 다른 작업과 병행할 때는 False 권장 (DOM 직접 읽기가 1차 경로라 대부분 불필요)
ALLOW_CLIPBOARD_FALLBACK = False

# ── 파일명 템플릿 ──
#   placeholder: {panel} {viz} {ts} {idx}
#   "{panel}__{viz}"       → API 추출용__B2C_Device2 (1).json   (기본)
#   "{panel}__{idx:02d}"   → API 추출용__01.json
#   "{panel}__{viz}__{ts}" → 시각까지 포함 (v10 방식)
FILENAME_TEMPLATE = "{panel}__{viz}"

# 재실행 시 이미 완료된 패널은 펼치지도 않고 통째로 건너뜀 (2회전 전략에 최적)
SKIP_COMPLETED_PANELS = True

EXISTING_FILES: set[str] = set()   # run()에서 채움 (viz마다 디스크 조회 방지)
COMPLETED_PANELS: set[str] = set() # 이전 실행에서 완료된 패널명


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
        dbg_dir = SAVE_DIR / "_debug"
        dbg_dir.mkdir(parents=True, exist_ok=True)
        (dbg_dir / f"popover_{tag}.html").write_text(html, encoding="utf-8")
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


def js_click(loc) -> None:
    """
    좌표 기반 클릭 대신 요소에 직접 click 이벤트 발사.
    레이아웃이 밀려 좌표에 옆 버튼(설정 등)이 오는 오클릭을 원천 차단.
    """
    loc.evaluate("el => el.click()")


def _click_first_visible(loc) -> bool:
    """locator 매칭 중 화면에 보이는 첫 요소에 JS 클릭. 성공 여부 반환."""
    try:
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            if el.is_visible():
                js_click(el)
                return True
    except Exception:
        pass
    return False


def neutralize_blockers(frame) -> None:
    """클릭을 가로채는 오버레이(aa-scroll-cover, Tip of the Day) 무력화."""
    try:
        frame.evaluate(
            """() => {
                const s = document.createElement('style');
                s.textContent =
                  '.aa-scroll-cover, .TipOfTheDay-container ' +
                  '{ display:none !important; pointer-events:none !important; }';
                document.head.appendChild(s);
            }"""
        )
        print("[*] 클릭 방해 오버레이(scroll-cover, TipOfTheDay) 무력화 완료")
    except Exception as e:
        print(f"[!] 오버레이 무력화 실패(계속 진행): {e}")


COPY_BTN_INDEX = 0  # Copy to clipboard 버튼이 여러 개면 몇 번째를 누를지
                    # (보통 0 = 왼쪽/위 = Request. Response가 저장되면 1로)

_CHUNK_JS = """(sel) => {
    // 1) 다이얼로그 안의 모든 스크롤 가능한 영역을 바닥까지 스크롤 (lazy 렌더 강제)
    for (const root of document.querySelectorAll(sel)) {
        if (!root.getClientRects().length) continue;
        for (const el of root.querySelectorAll('*')) {
            if (el.scrollHeight > el.clientHeight + 10) el.scrollTop = el.scrollHeight;
        }
    }
    // 2) 텍스트 수집
    const out = [];
    for (const el of document.querySelectorAll(sel)) {
        if (!el.getClientRects().length) continue;
        el.querySelectorAll('textarea').forEach(t => out.push(t.value || ''));
        el.querySelectorAll('pre, code').forEach(p => out.push(p.innerText || ''));
        out.push(el.innerText || '');
    }
    return out.filter(t => t && t.length > 20);
}"""


def _pick_request_json(chunks: list[str]) -> str | None:
    """청크들에서 'JSON Request' 마커 우선으로 첫 유효 오브젝트 추출."""
    if not chunks:
        return None
    ordered = sorted(
        chunks,
        key=lambda t: (0 if re.search(r"JSON\s*Request", t, re.I) else 1, -len(t)),
    )
    for t in ordered:
        m = re.search(r"JSON\s*Request", t, re.I)
        scope = t[m.end():] if m else t
        objs = _scan_json_objects(scope) or _scan_json_objects(t)
        if objs:
            return objs[0]
    return None


# 저비용 스캔: textarea.value / pre / code 만 직접 조회.
# innerText 를 쓰지 않으므로 레이아웃 재계산(reflow)이 없어 대형 DOM에서도 빠름.
_FAST_JS = """() => {
    const out = [];
    document.querySelectorAll('textarea').forEach(t => {
        if (t.value && t.value.length > 20) out.push(t.value);
    });
    document.querySelectorAll('pre, code').forEach(p => {
        const t = p.textContent || '';        // innerText 대신 textContent (reflow 없음)
        if (t.length > 20) out.push(t);
    });
    return out;
}"""


def extract_json_in_overlay(frame, deep: bool = False) -> str | None:
    """
    Oberon XML 뷰에서 JSON Request 추출.
    deep=False: 저비용 스캔(textarea/pre/code, reflow 없음) — 대부분 여기서 성공
    deep=True : 강제 스크롤 + innerText + 자식 frame까지 훑는 고비용 스캔(폴백)
    """
    try:
        chunks = frame.evaluate(_FAST_JS)
    except Exception:
        chunks = []
    hit = _pick_request_json(chunks)
    if hit or not deep:
        return hit

    chunks = []
    for scope_sel in ("body", SEL["overlay"]):
        try:
            chunks += frame.evaluate(_CHUNK_JS, scope_sel)
        except Exception:
            pass
    for child in frame.child_frames:
        try:
            chunks += child.evaluate(_CHUNK_JS, "body")
        except Exception:
            continue
    return _pick_request_json(chunks)


def extract_via_copy_button(frame, page) -> str | None:
    """
    'JSON' 섹션 제목 뒤에 오는 첫 'Copy to clipboard' 버튼(= JSON Request용)을
    DOM 순서로 자동 탐지해 실제 클릭 → 클립보드 읽기.
    ('Copy all fields', cURL용 버튼과 혼동 방지)
    """
    # JSON 헤딩 뒤 첫 번째 copy 버튼의 인덱스를 DOM에서 계산
    try:
        info = frame.evaluate(
            """() => {
                const btns = [...document.querySelectorAll('button')].filter(b =>
                    /copy to clipboard/i.test(b.textContent || '') &&
                    !/all fields/i.test(b.textContent || ''));
                let head = null;
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0 &&
                        (el.textContent || '').trim() === 'JSON') { head = el; break; }
                }
                if (!head) return { count: btns.length, index: -1 };
                for (let i = 0; i < btns.length; i++) {
                    if (head.compareDocumentPosition(btns[i]) &
                        Node.DOCUMENT_POSITION_FOLLOWING)
                        return { count: btns.length, index: i };
                }
                return { count: btns.length, index: -1 };
            }"""
        )
    except Exception:
        return None
    if not info or info["count"] == 0:
        return None
    idx = info["index"] if info["index"] >= 0 else min(COPY_BTN_INDEX, info["count"] - 1)

    btns = frame.locator("button").filter(
        has_text=re.compile("copy to clipboard", re.I)
    ).filter(has_not_text=re.compile("all fields", re.I))
    try:
        page.bring_to_front()
        btns.nth(idx).scroll_into_view_if_needed(timeout=5_000)
        btns.nth(idx).click(timeout=8_000)  # 신뢰된 클릭 (클립보드 권한 필요)
        time.sleep(0.6)
        try:
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        txt = frame.evaluate("() => navigator.clipboard.readText()")
        if not txt:
            return None
        objs = _scan_json_objects(txt)
        return objs[0] if objs else _pick_request_json([txt])
    except Exception:
        return None


def process_viz(frame, page, viz, panel_name: str, vi: int) -> int:
    """
    viz 하나 처리:
      (접혀 있으면 펼침) → 디버그 버튼 → 'Freeform Table' → 시간대 클릭
      → 같은 다이얼로그 안의 Oberon XML 뷰에서 JSON Request 추출
    """
    viz_name = aria_name(viz, "Visualization: ", f"viz{vi:02d}")
    tag = f"{sanitize(panel_name, 'p')}_{viz_name}"

    # ── 이어받기: 이미 추출된 파일이 있으면 건너뜀 ──
    if SKIP_EXISTING:
        prefix = f"{panel_name}__{viz_name}"
        if any(nm.startswith(prefix) for nm in EXISTING_FILES):
            print(f"    [=] {viz_name}: 기존 파일 있음 → 건너뜀")
            return 0

    dbg = viz.locator(SEL["debug_btn"])
    if dbg.count() == 0:
        print(f"    [-] {viz_name}: 디버그 버튼 없음 (Text 등 비-Oberon 컴포넌트) → 건너뜀")
        return 0

    # ── 접힌 visualization 펼치기 (Expand 버튼이 디버그 버튼을 가림) ──
    viz_was_collapsed = False
    try:
        tog = viz.locator(".subpanel-collapse-expand-button")
        if tog.count() > 0:
            label = tog.first.get_attribute("aria-label") or ""
            if label.startswith("Expand"):
                js_click(tog.first)
                viz_was_collapsed = True
                time.sleep(VIZ_EXPAND_WAIT_S)
    except Exception:
        pass

    try:
        viz.scroll_into_view_if_needed(timeout=8_000)
    except Exception:
        pass

    # ── 디버그 메뉴 열기 → 시간대 목록 확보 (비어 있으면 재시도) ──
    # 막 펼친 패널은 Oberon 요청이 아직 발사 전이라 목록이 빌 수 있음
    ts, n = None, 0
    for attempt in range(TS_OPEN_TRIES):
        js_click(dbg.first)
        time.sleep(MENU_OPEN_WAIT_S)

        overlay = frame.locator(SEL["overlay"])
        lvl1 = overlay.get_by_text(re.compile(FIRST_LEVEL_TEXT, re.I))
        if lvl1.count() > 0:
            if not _click_first_visible(lvl1):
                page.keyboard.press("Escape")
                time.sleep(1.5)
                continue
            time.sleep(LVL1_WAIT_S)

        ts = frame.locator(SEL["overlay"]).get_by_text(re.compile(TIMESTAMP_RE))
        n = ts.count()
        if n > 0:
            break
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        if attempt < TS_OPEN_TRIES - 1:
            time.sleep(TS_RETRY_WAIT_S)

    if n == 0:
        print(f"    [-] {viz_name}: 시간대 항목 0개 (재시도 {TS_OPEN_TRIES}회) → 덤프 후 건너뜀")
        dump_popover_debug(frame, f"{tag}_lvl2")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        return 0

    idx = 0 if TIMESTAMP_PICK == "first" else n - 1
    try:
        ts_label = sanitize(ts.nth(idx).inner_text()[:30], "latest")
    except Exception:
        ts_label = "latest"

    pages_before = set(page.context.pages)
    try:
        js_click(ts.nth(idx))
    except Exception as e:
        print(f"    [-] {viz_name}: 시간대 클릭 실패: {e}")
        dump_popover_debug(frame, f"{tag}_ts_click")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        return 0

    # ── 추출 1: DOM 직접 읽기 폴링 (강제 스크롤 + iframe 포함, 최대 ~8초) ──
    data = None
    for attempt in range(EXTRACT_POLLS):
        # 앞 3회는 저비용 스캔, 이후에만 고비용 전체 스캔으로 승격
        data = extract_json_in_overlay(frame, deep=(attempt >= 3))
        if data:
            break
        time.sleep(EXTRACT_POLL_S)
    # ── 추출 2: Copy to clipboard 버튼 경로 (병행 작업 시 꺼둘 것) ──
    if not data and ALLOW_CLIPBOARD_FALLBACK:
        data = extract_via_copy_button(frame, page)
        if data:
            print(f"    [i] {viz_name}: 클립보드 경로로 추출 성공")
    # ── 추출 3: 새 창 폴백 ──
    if not data:
        new_pages = [p for p in page.context.pages if p not in pages_before]
        if new_pages:
            data = extract_json_from_popup(new_pages[-1])

    saved = 0
    if data:
        stem = sanitize(
            FILENAME_TEMPLATE.format(
                panel=panel_name, viz=viz_name, ts=ts_label, idx=vi
            ),
            f"{panel_name}__{vi:02d}",
        )
        fname = SAVE_DIR / f"{stem}.json"
        if fname.exists():                    # 동일 이름 충돌 시 인덱스 부여
            fname = SAVE_DIR / f"{stem}_{vi:02d}.json"
        EXISTING_FILES.add(fname.name)
        fname.write_text(data, encoding="utf-8")
        print(f"    [+] 저장: {fname.name}")
        saved = 1
    else:
        print(f"    [-] {viz_name}: Oberon 뷰에서 JSON 추출 실패 → 덤프")
        dump_popover_debug(frame, f"{tag}_oberon")

    # Oberon 뷰 닫기: Cancel 버튼 우선, Escape 폴백
    try:
        cancel = frame.locator("button").filter(
            has_text=re.compile(r"^\s*Cancel\s*$", re.I)
        )
        if cancel.count() > 0:
            js_click(cancel.first)
            time.sleep(CLOSE_WAIT_S)
    except Exception:
        pass
    for _ in range(3):
        page.keyboard.press("Escape")
        time.sleep(0.15)

    # 원래 접혀 있던 viz는 다시 접기 (부하 관리)
    if viz_was_collapsed:
        try:
            js_click(viz.locator(".subpanel-collapse-expand-button").first)
            time.sleep(0.5)
        except Exception:
            pass
    return saved


def wait_for_panel_render(panel) -> None:
    """
    패널 펼침 후: viz 개수가 안정될 때까지 폴링(연속 3회 동일)
    → 이후 viz 수에 비례한 데이터 로딩 유예 (상한 있음).
    """
    deadline = time.time() + PANEL_RENDER_WAIT_S
    prev, stable, n = -1, 0, 0
    while time.time() < deadline:
        try:
            n = panel.locator(SEL["viz"]).count()
        except Exception:
            n = 0
        if n > 0 and n == prev:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev = n
        time.sleep(0.5)
    grace = min(PANEL_BASE_WAIT_S + PER_VIZ_WAIT_S * n, PANEL_MAX_WAIT_S)
    print(f"    (viz {n}개 렌더 확인 → 데이터 로딩 {grace:.0f}s 대기)")
    time.sleep(grace)


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

        # ── 워크스페이스 이름으로 하위 저장 폴더 구성 ──
        global SAVE_DIR
        try:
            title = frame.evaluate("() => document.title") or ""
        except Exception:
            title = ""
        ws_name = sanitize(
            re.sub(r"\s*-\s*Analysis Workspace\s*$", "", title.strip()), "workspace"
        )
        SAVE_DIR = OUTPUT_DIR / ws_name
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[*] 저장 폴더: {SAVE_DIR}")

        # 기존 산출물 캐시 (viz마다 디스크를 뒤지지 않도록 1회만 로드)
        EXISTING_FILES.update(f.name for f in SAVE_DIR.iterdir() if f.is_file())
        mf = SAVE_DIR / "_manifest.json"
        if mf.exists():
            try:
                prev = json.loads(mf.read_text(encoding="utf-8"))
                COMPLETED_PANELS.update(
                    e["name"] for e in prev.get("panels", [])
                    if e.get("complete")
                )
            except Exception:
                pass
        if EXISTING_FILES:
            print(f"[*] 기존 파일 {len(EXISTING_FILES)}개 / 완료 패널 "
                  f"{len(COMPLETED_PANELS)}개 → 건너뜀 대상")

        neutralize_blockers(frame)

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

            # 이전 실행에서 완료된 패널: 펼치는 비용 자체를 회피 (재실행 대폭 단축)
            if SKIP_COMPLETED_PANELS and panel_name in COMPLETED_PANELS:
                print("    [=] 이전 실행에서 완료됨 → 패널 통째로 건너뜀")
                continue

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
                    got = 0
                    for r in range(VIZ_RETRIES + 1):
                        try:
                            got = process_viz(
                                frame, page, vizzes.nth(vi), panel_name, vi + 1
                            )
                            break
                        except Exception as e:
                            if r < VIZ_RETRIES:
                                print(f"    [!] viz{vi+1:02d} 예외 → 재시도: {e}")
                            else:
                                p_entry["errors"] += 1
                                print(f"    [-] viz{vi+1:02d} 예외 → 건너뜀: {e}")
                            try:
                                for _ in range(3):
                                    page.keyboard.press("Escape")
                            except Exception:
                                pass
                            time.sleep(2)
                    total += got
                    p_entry["viz_saved"] += got

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

            p_entry["complete"] = (p_entry["errors"] == 0)
            manifest["panels"].append(p_entry)
            manifest["total_saved"] = total
            # 패널마다 증분 저장 → 중간에 끊겨도 진행 상황 보존
            (SAVE_DIR / "_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        manifest["total_saved"] = total
        manifest["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (SAVE_DIR / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[완료] 총 {total}개 JSON 저장 → {SAVE_DIR.resolve()}")
        print("       요약: output_json/_manifest.json")


if __name__ == "__main__":
    run()
