# ESPN 판타지 농구 로스터 재편 리포트

ESPN 판타지 농구(포인트 리그) 내 팀을 분석해 방출→영입 재편안, 포지션 구성, 이번 매치업 승률, 영입 후보를 한 화면으로 보여 줍니다. LLM 없이 통계 계산만 씁니다.

- 3시간마다(한국시간 1:17·4:17·7:17·…·22:17) GitHub Actions가 새로 만들어 GitHub Pages에 올립니다. 바로 만들려면 Actions → 리포트 만들기 → Run workflow.
- 재편안 아래 알약의 ×를 누르면 그 선수를 빼고 다시 계산합니다(다른 팀이 먼저 데려갔을 때).
- 비공개 리그라 ESPN 로그인 쿠키 `ESPN_S2`, `SWID`를 저장소 Secrets로 둡니다. 쿠키가 만료되면 Actions가 실패합니다. PC에서 `python sync_cookies.py`를 실행하면(만료됐으면 크롬 로그인 창이 뜸) 새 쿠키가 Secrets에 자동으로 들어갑니다.

PC에서 직접 실행: `pip install -r requirements.txt` 후 `python report.py` (설치된 크롬으로 한 번 로그인). 검사: `python check.py`.
