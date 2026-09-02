# aa_oberon_extractor

Adobe Analytics Workspace가 화면 안에 숨겨둔 **Oberon JSON 요청**을, 사람이 일일이 디버그 버튼
누르는 대신 브라우저 뒤에서 대신 눌러주는 자동화 스크립트.

## 이게 뭐 하는 애냐면

Workspace 프로젝트를 열면 패널/시각화마다 벌레 모양 디버그 버튼(🐛)이 숨어있고, 누르면 그
시각화가 실제로 API에 던진 Oberon 요청 JSON을 볼 수 있다. 이걸 패널 개수만큼 반복 클릭하는 건
사람이 할 짓이 아니라서, Playwright로 이미 켜져 있는 크롬(CDP)에 빙의해 대신 눌러준다.

- 패널을 하나씩 펼치고 → 안의 시각화들 디버그 버튼 클릭 → 팝오버에서 JSON 링크 찾아 새 창 열기
  → 저장 → 원래 접혀있었으면 도로 접기
- 패널/시각화 이름은 `aria-label`에서 그대로 뽑아옴
- 셀렉터를 못 찾으면 죽지 않고 `output_json/_debug/`에 HTML 덤프 후 계속 진행 (다음 세션 셀렉터
  보정용)

## 버전 안내

v2 → v3 → v4 → v10 → v12 순으로 진화했다. **v12가 가장 최신/안정판**이고, 나머지는 히스토리 겸
비교용으로 같이 보관 중이다.

## 실행 전 준비

1. 크롬을 전부 종료하고 디버그 포트로 다시 켠다.
   ```bash
   "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
       --remote-debugging-port=9222 --user-data-dir=C:\chrome-debug-profile
   ```
2. 그 크롬에서 SSO 로그인 → Workspace 프로젝트 열기 → Enable Debugger 켜기 (벌레 아이콘이
   보여야 한다).
3. ```bash
   python aa_oberon_extractor_v12.py
   ```

## 필요한 것

- Python 3.x
- `playwright` (`pip install playwright && playwright install chromium`)

---

*이건 화면을 대신 클릭해주는 스크립트지 정식 API 클라이언트가 아니다 — Adobe가 UI를 바꾸면
셀렉터도 같이 늙는다.*
