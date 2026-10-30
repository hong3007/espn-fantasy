"""ESPN 접속 없이 가짜 데이터로 report.py 로직 검사: python check.py"""
import json, re, shutil, subprocess
from collections import Counter
from datetime import date, datetime
from types import SimpleNamespace as NS
import report


def P(name, est, elig, days, slot="BE", injury="ACTIVE", back=0, waiver=False):
    sd = .3 * est
    return NS(name=name, playerId=name, est=est, dur=1, eligibleSlots=elig, schedule={str(d): {} for d in days},
              injuryStatus=injury, lineupSlot=slot, position=elig[0], proTeam="BOS", back_sp=back, waiver=waiver,
              avg_points=est, gp=5, last_avg=est, last_gp=70, projected_avg_points=est,
              sd=sd, se=sd / (5 + report.K) ** .5)


# 내 팀 찾기 (SWID 대소문자·중괄호 무시)
me_t = NS(team_name="<b>내팀</b>", owners=[{"id": "{ABC-123}"}], wins=0, losses=0, ties=0, points_for=1.0, roster=[])
other = NS(team_name="친구팀", owners=[{"id": "{XYZ}"}], wins=0, losses=0, ties=0, points_for=1.0, roster=[])
assert report.find_my_team([other, me_t], "{abc-123}") is me_t

# 지난 시즌 기록 읽기 + 경기당 추정(올 시즌 0경기면 사전 추정, K경기면 반반)
raw = {"player": {"stats": [{"id": "002026", "appliedAverage": 40.04, "stats": {"42": 50.0}}]}}
assert report.last_season(raw, 2026) == (40.0, 50) and report.last_season({}, 2026) == (0.0, 0)
assert report.estimate(60, 0, 40, 50, 50) == 45
assert report.estimate(60, report.K, 40, 50, 50) == 52.5
assert report.estimate(60, 0, 40, 5, 0) == 60, "지난 시즌 10경기 미만·예상 없음이면 올 시즌 평균 그대로"

# prepare: 복귀일(지난 날짜면 내일), 출전율(ESPN 예상 없으면 지난 시즌), 빈 부상 값, 이미 시작한 경기 제외
T = date(2026, 10, 29)
R = lambda inj, ret, sched=None: NS(stats={}, avg_points=0, projected_avg_points=0, injuryStatus=inj,
                                    expected_return_date=ret, schedule=sched or {})
assert [report.prepare(R("OUT", r), {}, 2027, 10, T).back_sp for r in (date(2026, 11, 3), None, date(2026, 10, 1))] == [15, 17, 11]
assert report.prepare(R("ACTIVE", None), raw, 2027, 10, T).dur == 50 / 82
assert report.prepare(R([], None), {}, 2027, 10, T).injuryStatus is None
sched = {"9": {"date": datetime(2000, 1, 1)}, "10": {"date": datetime(2999, 1, 1)}, "11": {}}
assert set(report.prepare(R("ACTIVE", None, sched), {}, 2027, 10, T).schedule) == {"10", "11"}

# 최적 선발: 욕심내서 G에 먼저 넣으면 틀리는 경우
assert report.best_lineup([(10, ["PG", "G"]), (8, ["G"])], ("PG", "G"))[0] == 18
assert report.best_lineup([(10, ["PG", "G"]), (9, ["PG", "G"]), (8, ["G"])], ("PG", "G"))[0] == 19
dtd = P("DTD", 10, ["PG"], range(1, 7), injury="DAY_TO_DAY")
assert [report.game_value(dtd, d, 1) for d in (1, 3, 4)] == [7.0, 7.0, 10], "출전 불투명은 오늘부터 사흘 70%"

# 시나리오: 선발 PG·C, 6일. 내 C는 1일만 경기 → C가 5일 빈다
slots, sps = ("PG", "C"), range(1, 7)
g1, c1, w = P("G1", 30, ["PG", "G"], sps), P("C1", 20, ["C"], [1]), P("W", 10, ["PG", "G"], sps)
roster = [g1, c1, w]
assert report.simulate(roster, sps, slots, 1).total == 30 * 6 + 20

c2 = P("C2", 25, ["C"], sps)
hurt = P("Hurt", 100, ["C"], sps, injury="OUT", back=6)  # 6일부터 복귀
bad = P("Bad", 5, ["PG", "G"], sps)
adds = report.rank_adds(roster, roster, [c2, hurt, bad], sps, slots, 1, {})
# C2↔W: 1일 C 20→25(+5), 2~6일 빈 C에 25씩(+125). Hurt는 6일에만 복귀(+100). Bad는 내 누구보다 약해 제외
assert [(a.name, d.name, round(g)) for a, d, g, _ in adds] == [("C2", "W", 130), ("Hurt", "W", 100)]
assert all(.5 < c <= 1 for *_, c in adds), "이득이 양수면 확실도는 50% 초과"

# 흔들림·확실도·승률 계산 부품
logs = {"player": {"stats": [{"statSourceId": 0, "statSplitTypeId": 5, "appliedTotal": x, "stats": {"40": 30}} for x in (10, 20, 30)]
                   + [{"statSourceId": 0, "statSplitTypeId": 5, "appliedTotal": 0, "stats": {}},       # 결장 경기
                      {"statSourceId": 1, "statSplitTypeId": 5, "appliedTotal": 99, "stats": {"40": 1}}]}}  # 예상치
assert report.game_cv(logs) == (0.5, 3) and report.game_cv({}) == (0.0, 0)
sp = [NS(cv=.5, cv_n=10, est=40, gp=0), NS(cv=.3, cv_n=10, est=40, gp=0), NS(cv=0.0, cv_n=0, est=50, gp=0)]
assert round(report.set_spread(sp), 2) == .4 and round(sp[0].sd, 6) == 18 and round(sp[2].sd, 6) == 20, "공통값 쪽으로 당김"
one = NS(playerId="X", sd=10, se=2)
assert report.spread(report.ZERO, NS(starts=Counter({"X": 2})), [one]) == 216 ** .5, "2경기 × 10² + (2×2)²"
assert report.prob_above(0, 5) == .5 and report.prob_above(3, 0) == 1.0 and report.prob_above(-1, 0) == 0.0
assert report.rank_adds(roster, roster, [c2], sps, slots, 1, {"C": 1})[0][1] is c1, "C 인원 제한이면 C를 내보내야 함"

# 재편안: 교체할 때마다 다시 계산. 2번째는 C2가 들어온 로스터 기준이라 C4↔C1(6일만 경기, C 25→100, +75)
# 결장 중인 Hurt는 계산상 이득이 있어도 영입 한도를 낭비하므로 추천에서 빠진다
c4 = P("C4", 100, ["C"], [6])
out = P("Out", 40, ["PG", "G"], sps, injury="OUT", back=99)
a = report.analyze(roster + [out], [], [c2, hurt, c4, bad], slots, {}, 1, 1, 3, 6)
assert [(f.name, d.name, round(g)) for f, d, g, _ in a.moves] == [("C2", "W", 130), ("C4", "C1", 75)]
assert round(a.new.total - a.base.total) == 205
assert all(t[0] is not hurt for t in a.adds), "결장 FA는 추천하지 않음"
# 포지션: PG는 W 방출로 3→2명, C는 C1 방출·C2·Hurt 영입으로 1→2명. 보강 효과는 그 포지션 최선 FA(C2 +130), PG는 없음
assert [(r.x, len(r.before), len(r.after), r.best and r.best[0].name) for r in a.pos] == [("PG", 3, 2, None), ("C", 1, 2, "C2")]
assert round(a.idle, 2) == round((2 * 6 - 7) / 6, 2), "선발 2자리×6일 중 7번만 채움(G1 6 + C1 1)"
assert any(x.startswith("Out: OUT 상태예요") for x in a.acts), a.acts

# 상대가 있고 지는 중: 이번 매치업용 추천은 웨이버 제외, IR 복귀 안내
ir = P("IRback", 25, ["PG"], sps, slot="IR")
other.roster = [P("O1", 50, ["PG"], sps), P("O2", 50, ["C"], sps)]
c2w, c3 = P("C2", 25, ["C"], sps, waiver=True), P("C3", 22, ["C"], sps)
a2 = report.analyze(roster + [ir], other.roster, [c2w, c3], slots, {}, 1, 1, 3, 6, scores=(150, 0),
                    acq=NS(limit=1, used=0, next_start=date(2026, 11, 2)))
t2 = "\n".join(a2.acts)
assert (a2.me_w.total, a2.opp_w.total) == (110, 300)
# 예상 최종 260 대 300 → 40점 차라 승률 낮음. C3↔W(바로 영입)는 1~3일 +2+22+22=46점이라 승률이 크게 오름
assert .1 < a2.win < .3, a2.win
assert a2.moves[0][0] is c2w and re.search(r"승률 \d+% → \d+%: C3 영입", t2) and "IRback: IR에서 복귀" in t2, t2
a3 = report.analyze(roster + [ir], other.roster, [c2w, c3], slots, {}, 1, 1, 3, 6, scores=(0, 0))
assert a3.win < .01 and not any("승률" in x for x in a3.acts), "190점 차는 46점으로 승률이 거의 안 오르니 1순위를 미루라고 하면 안 됨"

# 브라우저 재편안(PLAN_JS)이 파이썬 plan_moves와 같은 답인지 node로 비교. 알약 ×로 C2를 뺀 경우도 파이썬 재계산과 비교
node = shutil.which("node")
if node:
    js = ("const D=" + json.dumps(report.plan_data(a)) + ";" + report.PLAN_JS +
          "console.log(JSON.stringify([[], ['C2']].map(x => plan(new Set(x)).moves.map(m => [m.fa.n, m.d.n, m.g, m.c]))))")
    got = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout)
    b = report.analyze(roster + [out], [], [hurt, c4, bad], slots, {}, 1, 1, 3, 6)  # C2 없이 파이썬으로
    for js_moves, py_moves in zip(got, (a.moves, b.moves)):
        assert [m[:2] for m in js_moves] == [[f.name, d.name] for f, d, *_ in py_moves], (js_moves, py_moves)
        assert all(abs(m[2] - g) < 1e-9 and abs(m[3] - c) < 1e-6 for m, (_, _, g, c) in zip(js_moves, py_moves))
    # 실제 페이지처럼 처음 재편안(init)을 넣으면 계산 없이 그 값이 그대로 나와야 함(id → 선수 연결)
    d0 = report.plan_data(a)
    d0["init"] = {"moves": [[str(f.playerId), str(d.playerId), g, c] for f, d, g, c in a.moves], "base": 1.0, "after": 2.0}
    js = ("const D=" + json.dumps(d0) + ";" + report.PLAN_JS +
          "const P = plan(new Set()); console.log(JSON.stringify([P.moves.map(m => [m.fa.n, m.d.n]), P.base, P.after]))")
    names, base0, after0 = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout)
    assert names == [[f.name, d.name] for f, d, *_ in a.moves] and (base0, after0) == (1.0, 2.0), names
    # 첫 화면(미리 계산해 둔 값)과 × 후 다시 계산한 값이 같은 기준인지: JS 합계가 파이썬 합계와 같아야 함
    js = ("const D=" + json.dumps(report.plan_data(a)) + ";" + report.PLAN_JS +
          "const P = plan(new Set(['none'])); console.log(JSON.stringify([P.base, P.after]))")
    jb, ja = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout)
    assert abs(jb - a.base.total) < 1e-9 and abs(ja - a.new.total) < 1e-9, (jb, ja, a.base.total, a.new.total)
    # 하루 31명 이상이 뛰면 조용히 틀리지 말고 멈춘다
    js = ("const D={my:[],ir:[],fa:[]};" + report.PLAN_JS +
          "let ok = 0; for (const f of [score, lineup]) { try { f(Array(31).fill([1, ['PG']]), ['PG']) } catch (e) { ok++ } } console.log(ok)")
    assert subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout.strip() == "2"
else:
    print("node가 없어 브라우저 재편안 비교는 건너뜀")

# 화면: 현재+남은 예상, 팀 이름·부상 문구 이스케이프, 아이콘, BYE
me_t.roster = roster + [ir]
w.injuryStatus = "<img src=x>"
box = NS(home_team=other, away_team=me_t, home_score=111.0, away_score=99.0)
page = report.render("우리리그", [me_t, other], me_t, box, other, a2)
assert "<p class=final>209</p>" in page and "<p class=final>411</p>" in page, "현재 점수 + 남은 예상"
assert "방출 → C2" in page and "id=pills" in page and "내 승률" in page and "80% 범위" in page
assert page.count("<script>") == page.count("</script>") == 2, "데이터 안의 </ 가 스크립트를 끊으면 안 됨"
assert re.search(r'name=generated content="\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"', page) and "id=upd" in page and "id=updmsg" in page, "새 리포트 확인 버튼"
bye = report.render("우리리그", [me_t, other], me_t, None, None, a)
assert '과다</span> → <span class="tag spare">적정' in bye and '부족</span> → <span class="tag spare">적정' in bye, "PG 3→2, C 1→2"
assert "&lt;b&gt;내팀" in page and "<b>내팀" not in page and "<img src=x>" not in page, "ESPN 문자열은 이스케이프"
assert "data:image/png;base64," in page and "apple-touch-icon" in page and "영입 <b>1회 남음</b>" in page and "(0회 사용 / 한도 1회)" in page
assert "BYE" in bye

# 영입 한도: 남은 횟수에 맞춰 주별 배정, 다 썼으면 다음 매치업부터 계산
mk = lambda limit, used: report.analyze(roster + [out], [], [c2, c4, bad], slots, {}, 1, 1, 3, 6,
                                        acq=NS(limit=limit, used=used, next_start=date(2026, 11, 2)))
assert mk(1, 0).week_of == [0, 1, 2] and mk(1, 0).sps.start == 1
x = mk(1, 1)
assert x.left == 0 and x.week_of == [1, 2, 3] and x.sps.start == 4, (x.left, x.week_of, x.sps)
assert mk(2, 1).week_of == [0, 1, 1] and mk(-1, 0).week_of == [0, 0, 0]
assert mk(1, 7).left == 0, "한도보다 많이 썼어도 남은 횟수는 0"
px = report.render("우리리그", [me_t, other], me_t, None, None, x)
assert 'class="quota zero"' in px and "<b>0회 남음</b>" in px and "다음 매치업(11/2 월)부터예요" in px, "다 썼으면 안내"
assert px.index("class=\"quota zero\"") < px.index("id=pills"), "한도 안내는 재편안(알약) 위에"
assert "다음 주 11/2~" in px and "그다음 주 11/9~" in px and "다음 매치업(11/2)부터 " in px, "주별 날짜 표기"
assert "한도보다 많이 영입" in report.render("우리리그", [me_t, other], me_t, None, None, mk(1, 7)), "설정과 기록이 어긋날 때 알림"
assert report.icon_png()[:8] == b"\x89PNG\r\n\x1a\n"

# 새 리포트 확인: 다음 자동 실행 시각(한국시간 1·4·…·22시 17분)을 올바로 계산하는지 node로
if node:
    js = report.UPD_JS + "const K = (h, m) => Date.UTC(2026, 9, 31, h - 9, m); console.log(JSON.stringify([nextSlot(K(1, 4)), nextSlot(K(1, 17)), nextSlot(K(1, 18)), nextSlot(K(12, 45)), nextSlot(K(22, 30)), nextSlot(K(0, 0))]))"
    got = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert got == ["1시 17분", "4시 17분", "4시 17분", "13시 17분", "내일 1시 17분", "1시 17분"], got
# 자동 실행 시각이 workflow cron(UTC)과 같은 목록인지
cron = re.search(r'cron: "17 ([\d,]+) \* \* \*"', (report.HERE / ".github/workflows/report.yml").read_text(encoding="utf-8")).group(1)
assert sorted((int(h) + 9) % 24 for h in cron.split(",")) == [int(x) for x in re.search(r"SLOTS = \[([\d, ]+)\]", report.UPD_JS).group(1).split(",")], "UPD_JS SLOTS와 cron이 다름"

# CI 설치 목록은 requirements.txt의 부분집합(버전이 서로 어긋나면 안 됨)
ci = set((report.HERE / "requirements-ci.txt").read_text(encoding="utf-8").split())
assert ci <= set((report.HERE / "requirements.txt").read_text(encoding="utf-8").split()), "requirements-ci.txt가 requirements.txt와 다름"

# 보안: 로그인 창에서 입력값·화면·통신을 엿볼 수 있는 Playwright 기능을 쓰면 실패
src = open(report.__file__, encoding="utf-8").read()
snoop = re.findall(r"\.on\(|\.route\(|evaluate|expose_|tracing|record_|screenshot|keyboard|input_value|inner_text|\.content\(|add_init_script|\.request\b", src)
assert not snoop, f"report.py에 엿보기 기능 사용: {snoop}"
used = set(re.findall(r"\b(?:ctx|page)\.(\w+)", src))  # 허용 목록: 쿠키 읽기와 창 열고 닫기만
assert used <= {"cookies", "close", "clear_cookies", "pages", "new_page", "goto", "wait_for_timeout"}, used
print("OK")
