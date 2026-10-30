"""ESPN 판타지 농구(포인트 리그) 로스터 재편 리포트 → report.html

목적: 영입↔방출로 내 선수 구성을 바꿔 선발 점수를 최대로 올린다.

선수 가치 = 경기당 추정 점수(올 시즌 + 지난 시즌 + ESPN 예상) × 출전율.
날짜마다 실제 NBA 일정으로 이 리그 선발 슬롯에 최적 배치해 '선발로 뛰어서 얻는 점수'로 비교한다.
재편안 = 매치업당 영입 한도에 맞춰, 앞으로 WINDOW일 선발 점수를 가장 많이 올리는 교체를 순서대로.
확실도·승률 = 선수별 경기 변동(경기 기록)과 실력 추정 오차로 정규분포 근사. LLM은 쓰지 않는다.
"""
import base64, html, json, os, struct, webbrowser, zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist, fmean, median, stdev
from types import SimpleNamespace as NS
from urllib.parse import quote

HERE = Path(__file__).parent
K = 10       # 올 시즌 이 경기 수만큼 뛰면 올 시즌 평균과 사전 추정(지난 시즌·ESPN 예상)을 반반 믿는다
WINDOW = 21  # 영입 효과를 보는 기간(일). 매치업당 영입 한도가 작아 한 번 데려오면 몇 주는 쓴다고 본다
MOVES = 3    # 재편안 교체 수(매치업당 1명이면 3주치)
MIN_GAIN = 5  # 이 점수(WINDOW일 선발 점수)보다 적게 오르는 교체는 영입 한도를 쓸 가치가 없어 추천하지 않는다
CV_M = 10    # 선수 개인의 경기 변동계수를 리그 공통값 쪽으로 당기는 강도(경기 수)
STATUS = {"OUT": "OUT", "DAY_TO_DAY": "DTD", "SUSPENSION": "SUSP"}  # ESPN 판타지에서 쓰는 용어 그대로(사용자 요청)

ICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<circle cx='32' cy='32' r='30' fill='#f07a1a'/>"
            "<path d='M2 32h60M32 2v60M12 10A34 34 0 0 1 12 54M52 10A34 34 0 0 0 52 54' "
            "fill='none' stroke='#2b1a0e' stroke-width='3'/></svg>")


def icon_png(size=192):
    """ICON_SVG와 같은 농구공 모양 PNG (모바일은 SVG 아이콘을 안 씀)"""
    raw = b""
    for py in range(size):
        raw += b"\0"
        for px in range(size):
            x, y = (px + .5) * 64 / size, (py + .5) * 64 / size
            if (x - 32) ** 2 + (y - 32) ** 2 > 900:
                raw += b"\0\0\0\0"
                continue
            seam = (abs(x - 32) < 1.5 or abs(y - 32) < 1.5
                    or abs(((x + 14) ** 2 + (y - 32) ** 2) ** .5 - 34) < 1.5
                    or abs(((x - 78) ** 2 + (y - 32) ** 2) ** .5 - 34) < 1.5)
            raw += b"\x2b\x1a\x0e\xff" if seam else b"\xf0\x7a\x1a\xff"
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def find_my_team(teams, swid):
    """SWID 쿠키 값 = ESPN 회원 ID. 그 회원이 주인인 팀이 내 팀"""
    key = swid.strip("{} ").upper()
    for t in teams:
        if any(o.get("id", "").strip("{}").upper() == key for o in t.owners):
            return t
    raise SystemExit("이 리그에서 내 팀을 못 찾았습니다. 다른 리그라면 .env에 LEAGUE_ID=리그번호 를 넣으세요.")


def last_season(raw, season):
    """원본 선수 데이터에서 지난 시즌 (경기당 판타지 점수, 출전 경기 수). 이 리그 점수 규칙으로 계산된 값"""
    pl = raw.get("playerPoolEntry", {}).get("player") or raw.get("player", {})
    for s in pl.get("stats", []):
        if s.get("id") == f"00{season}":
            return round(s.get("appliedAverage", 0), 1), int(s.get("stats", {}).get("42", 0))  # 42 = 출전 경기 수
    return 0.0, 0


def estimate(cur, gp, last_avg, last_gp, proj):
    """경기당 추정 점수: 시즌 초반 몇 경기로 튀는 올 시즌 평균을 지난 시즌 평균·ESPN 예상 쪽으로 당긴다"""
    prior = [v for v, ok in ((last_avg, last_gp >= 10), (proj, proj > 0)) if ok]
    prior = sum(prior) / len(prior) if prior else cur
    return (gp * cur + K * prior) / (gp + K)


def prepare(p, raw, year, today_sp, today):
    """선수에 분석값을 붙인다: est 경기당 추정, dur 출전 내구성(0.4~1), back_sp 부상 복귀 예상일(스코어링 기간 번호)"""
    total = p.stats.get(f"{year}_total", {}).get("total") or {}
    proj = p.stats.get(f"{year}_projected", {}).get("total") or {}
    p.gp = int(total.get("GP", 0))
    p.last_avg, p.last_gp = last_season(raw, year - 1)
    p.est = estimate(p.avg_points, p.gp, p.last_avg, p.last_gp, p.projected_avg_points)
    # ESPN 예상 출전 경기는 부상 이력을 이미 반영한다. 지난 시즌 출전만 쓰면 한 번 크게 다친 선수가 반값이 된다
    gp = proj.get("GP") or p.last_gp
    p.dur = min(1, max(.4, gp / 82)) if gp else .85  # ponytail: 자료 없는 신인은 85%로 가정
    p.waiver = raw.get("status") == "WAIVERS"
    p.injuryStatus = p.injuryStatus or None
    now = datetime.now()  # 이미 시작한 경기는 현재 점수에 들어가 있으니 남은 일정에서 뺀다
    p.schedule = {k: g for k, g in p.schedule.items() if not g.get("date") or g["date"] > now}
    p.back_sp = 0
    if p.injuryStatus in ("OUT", "SUSPENSION"):  # 복귀일을 모르면 1주 결장으로 가정
        p.back_sp = today_sp + (max(1, (p.expected_return_date - today).days) if p.expected_return_date else 7)
    p.cv, p.cv_n = game_cv(raw)
    return p


def game_cv(raw):
    """경기별 기록(올 시즌·지난 시즌, 결장 경기 제외)의 변동계수(표준편차÷평균)와 경기 수. 선수 카드에만 있다"""
    pl = raw.get("playerPoolEntry", {}).get("player") or raw.get("player", {})
    g = [s["appliedTotal"] for s in pl.get("stats", [])
         if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 5 and s.get("stats", {}).get("40")]  # 40 = 출전 시간
    return (stdev(g) / fmean(g), len(g)) if len(g) >= 2 and fmean(g) > 5 else (0.0, 0)


def set_spread(players):
    """선수별 sd(한 경기 점수의 흔들림)와 se(경기당 추정의 오차)를 붙인다.
    개인 변동계수는 경기 수가 적으면 리그 공통값(중앙값) 쪽으로 당긴다. se는 K경기 보정과 같은 가정(사전 추정 오차 = sd/√K)."""
    known = [p.cv for p in players if p.cv_n]
    common = median(known) if known else .35  # 2026-10-29 실제 리그 40명 중앙값 0.35
    for p in players:
        cv = (p.cv_n * p.cv + CV_M * common) / (p.cv_n + CV_M)
        p.sd = cv * p.est
        p.se = p.sd / (p.gp + K) ** .5
    return common


def spread(before, after, players):
    """선발 횟수 변화(after - before)로 생기는 점수 차의 표준편차: 늘거나 준 경기의 흔들림 + 선수별 추정 오차"""
    var = 0.0
    for p in players:
        d = after.starts[p.playerId] - before.starts[p.playerId]
        var += abs(d) * p.sd ** 2 + (d * p.se) ** 2
    return var ** .5


def prob_above(mean, sd):
    """정규분포 근사로 '0보다 클' 확률"""
    return 1 - NormalDist(mean, sd).cdf(0) if sd > 0 else float(mean > 0)


ZERO = NS(starts=Counter())  # spread()의 '아무도 안 뛴' 기준
ros = lambda p: p.est * p.dur  # 장기 가치: 경기당 추정 × 출전율


def game_value(p, sp, today_sp):
    """sp날 이 선수의 예상 점수. 경기가 없거나 부상 결장이면 0"""
    if str(sp) not in p.schedule or sp < p.back_sp:
        return 0
    return ros(p) * (.7 if p.injuryStatus == "DAY_TO_DAY" and sp < today_sp + 3 else 1)  # ponytail: 출전 불투명은 사흘간 30% 결장


def games(p, sps):
    return sum(str(sp) in p.schedule and sp >= p.back_sp for sp in sps)


def best_lineup(cands, slots):
    """그날 뛰는 선수들 [(점수, 가능한 슬롯)]을 선발 슬롯에 배치해 합계 최대. (합계, 슬롯마다 선수 번호 또는 None)"""
    @lru_cache(None)
    def f(i, used):
        if i == len(slots):
            return 0.0, ()
        s, rest = f(i + 1, used)
        best = (s, (None,) + rest)
        for j, (v, elig) in enumerate(cands):
            if not used >> j & 1 and slots[i] in elig:
                s, rest = f(i + 1, used | 1 << j)
                if s + v > best[0]:
                    best = (s + v, (j,) + rest)
        return best
    return f(0, 0)


def simulate(roster, sps, slots, today_sp):
    """날마다 최적 선발 → total 예상 점수 합, starts 선수별 선발 경기 수"""
    total, starts = 0.0, Counter()
    for sp in sps:
        playing = [(p, v) for p in roster if (v := game_value(p, sp, today_sp)) > 0]
        s, assign = best_lineup([(v, p.eligibleSlots) for p, v in playing], slots)
        total += s
        starts.update(playing[j][0].playerId for j in assign if j is not None)
    return NS(total=total, starts=starts)


BASE = ("PG", "SG", "SF", "PF", "C")  # 기본 포지션(구성판 칸)


def rank_adds(active, roster, fas, sps, slots, today_sp, limits):
    """FA마다 내 선수 1명과 바꿨을 때 sps 기간 선발 점수 순증가가 가장 큰 교체 [(FA, 방출, 증가, 확실도)].
    장기 가치가 FA보다 낮은 선수만 내보내서, 길게 봐도 손해 없는 교체만 남긴다. 확실도 = 증가가 0보다 클 확률."""
    base = simulate(active, sps, slots, today_sp)
    picks = []
    for fa in sorted(fas, key=ros, reverse=True)[:30]:  # ponytail: 장기 가치 상위 30명만 계산(속도). 숨은 선수를 놓치면 늘리기
        best = None
        for d in sorted((p for p in active if ros(p) < ros(fa)), key=ros)[:4]:
            if fa.position in limits and sum(p.position == fa.position for p in roster if p is not d) >= limits[fa.position]:
                continue  # 리그 포지션 인원 제한 초과
            sim = simulate([p for p in active if p is not d] + [fa], sps, slots, today_sp)
            if best is None or sim.total - base.total > best[2]:
                best = (fa, d, sim.total - base.total, sim)
        if best and best[2] > MIN_GAIN:
            picks.append(best[:3] + (prob_above(best[2], spread(base, best[3], active + [fa])),))
    return sorted(picks, key=lambda x: x[2], reverse=True)


def plan_moves(active, roster, fas, sps, slots, today_sp, limits, adds, n=MOVES):
    """순서대로 할 교체 n개. 교체할 때마다 바뀐 로스터로 다시 계산한다. (재편 후 로스터도 반환)
    브라우저용 PLAN_JS의 plan()과 같은 계산이다. 바꾸면 둘 다 바꿀 것."""
    moves = []
    for i in range(n):
        if i:
            adds = rank_adds(active, roster, fas, sps, slots, today_sp, limits)
        if not adds:
            break
        fa, d = adds[0][:2]
        moves.append(adds[0])
        active = [p for p in active if p is not d] + [fa]
        roster = [p for p in roster if p is not d] + [fa]
        fas = [p for p in fas if p is not fa]
    return moves, active


def analyze(roster, opp_roster, fas, slots, limits, ir_slots, today_sp, week_end, final_sp, scores=(0, 0), acq=None):
    """scores = 이번 매치업 현재 점수 (나, 상대).
    acq = 영입 한도 NS(limit 매치업당 한도(-1이면 무제한), used 이번 매치업에 쓴 횟수, next_start 다음 매치업 시작 날짜)"""
    acq = acq or NS(limit=-1, used=0, next_start=None)
    left = max(0, acq.limit - acq.used) if acq.limit > 0 else None  # 이번 매치업에 남은 영입 횟수(None=무제한)
    start = today_sp if left != 0 else week_end + 1  # 이번 매치업 영입을 다 썼으면 재편안은 다음 매치업부터 계산
    if start > final_sp:
        start = today_sp
    # 몇 번째 교체를 몇 주 뒤에 하는지: 이번 매치업 남은 횟수만큼은 이번 주, 이후는 매치업마다 한도만큼
    week_of = [0 if left is None or i < left else 1 + (i - left) // acq.limit for i in range(MOVES)]
    fas = [f for f in fas if f.injuryStatus not in ("OUT", "SUSPENSION")]  # 영입 한도가 귀해서 지금 못 뛰는 FA는 추천하지 않는다
    active = [p for p in roster if p.lineupSlot != "IR"]
    sps = range(start, min(start + WINDOW, final_sp + 1))
    week = range(today_sp, min(week_end, final_sp) + 1)
    base = simulate(active, sps, slots, today_sp)
    adds = rank_adds(active, roster, fas, sps, slots, today_sp, limits)
    moves, after = plan_moves(active, roster, fas, sps, slots, today_sp, limits, adds)
    # 포지션: 재편안이 인원을 늘리면 부족, 줄이면 과다였던 것. 보강 효과 = 그 포지션 FA 최선 영입의 순증가
    pos = [NS(x=x, before=[p for p in active if x in p.eligibleSlots], after=[p for p in after if x in p.eligibleSlots],
              best=next((t for t in adds if x in t[0].eligibleSlots), None)) for x in BASE if x in slots]
    idle = (len(slots) * len(sps) - sum(base.starts.values())) / max(1, len(sps))
    opp_active = [p for p in opp_roster if p.lineupSlot != "IR"]
    me_w = simulate(active, week, slots, today_sp)
    opp_w = simulate(opp_active, week, slots, today_sp) if opp_active else None
    win = diff = sd = None
    if opp_w:  # 승률: 현재 점수 + 남은 예상, 남은 선발 경기의 흔들림과 추정 오차로 정규분포 근사
        diff = scores[0] + me_w.total - scores[1] - opp_w.total
        sd = (spread(ZERO, me_w, active) ** 2 + spread(ZERO, opp_w, opp_active) ** 2) ** .5
        win = prob_above(diff, sd)

    acts = []  # 재편안 말고 함께 챙길 것
    ir_free = ir_slots - sum(p.lineupSlot == "IR" for p in roster)
    for p in [p for p in active if p.injuryStatus == "OUT"][:max(0, ir_free)]:
        acts.append(f"{p.name}: OUT 상태예요. IR로 옮기면 방출 없이 1명을 더 영입할 수 있어요.")
    for p in roster:
        if p.lineupSlot == "IR" and p.injuryStatus not in ("OUT", "SUSPENSION"):
            acts.append(f"{p.name}: IR에서 복귀했어요. 로스터로 올려야 영입이 막히지 않아요(1명 방출 필요).")
    if win is not None and win < .5 and left != 0:  # 이번 매치업에 영입이 남아 있을 때만
        wk = rank_adds(active, roster, [f for f in fas if not f.waiver], week, slots, today_sp, limits)[:1]
        if wk and (not moves or wk[0][0] is not moves[0][0]):
            new = prob_above(diff + wk[0][2], sd)
            if new - win >= .1:  # 승률이 10%p 이상 오를 때만 1순위를 미루라고 권한다
                acts.append(f"이번 매치업 승률 {win:.0%} → {new:.0%}: {wk[0][0].name} 영입, {wk[0][1].name} 방출이면 "
                            f"남은 기간 +{wk[0][2]:.0f}점이에요. 대신 재편안 1번은 다음 주로 밀려요.")
    return NS(slots=slots, days=len(sps), sps=sps, base=base, new=simulate(after, sps, slots, today_sp),
              moves=moves, adds=adds, pos=pos, idle=idle, acts=acts, me_w=me_w, opp_w=opp_w,
              roster=roster, cands=sorted(fas, key=ros, reverse=True)[:40], today_sp=today_sp, limits=limits,
              win=win, me_sd=spread(ZERO, me_w, active), opp_sd=spread(ZERO, opp_w, opp_active) if opp_w else 0,
              active=active, acq=acq, left=left, week_of=week_of)


def plan_data(a, info=lambda p: ""):
    """브라우저 재편안 계산(PLAN_JS)에 넘길 숫자. 이름·info는 HTML 이스케이프된 상태로 넣는다"""
    pack = lambda p: {"id": str(p.playerId), "n": html.escape(p.name), "pos": p.position, "info": info(p),
                      "elig": [s for s in dict.fromkeys(p.eligibleSlots) if s in a.slots], "ros": ros(p), "sd": p.sd, "se": p.se,
                      "val": [game_value(p, sp, a.today_sp) for sp in a.sps]}
    return {"slots": list(a.slots), "days": len(a.sps), "n": MOVES, "min": MIN_GAIN, "limits": a.limits, "my": [pack(p) for p in a.active],
            "ir": [{"id": str(p.playerId), "pos": p.position} for p in a.roster if p.lineupSlot == "IR"],
            "fa": [pack(p) for p in a.cands]}


# rank_adds·plan_moves를 그대로 옮긴 것. 알약 ×로 뺀 FA를 제외하고 재편안을 다시 계산해 그린다(check.py가 파이썬과 결과 비교)
PLAN_JS = r"""
D.roster = D.my.concat(D.ir);
const cdf = z => { const t = 1 / (1 + .3275911 * Math.abs(z) / Math.SQRT2),
  y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - .284496736) * t + .254829592) * t * Math.exp(-z * z / 2);
  return z >= 0 ? (1 + y) / 2 : (1 - y) / 2; };
const above = (m, s) => s > 0 ? cdf(m / s) : +(m > 0);
// 하루 뛰는 선수가 31명 미만이라는 가정(비트 연산). 실제로는 로스터 12~13명 + FA 1명
function lineup(c, S) {  // 점수와 배치(선발 횟수가 필요할 때만: 확실도 계산용)
  if (c.length > 30) throw new Error("하루에 뛰는 선수가 31명 이상");
  const m = S.length + 1, memo = new Map();
  const f = (i, u) => {
    if (i === S.length) return [0, []];
    const k = u * m + i;
    if (memo.has(k)) return memo.get(k);
    const [s, r] = f(i + 1, u); let b = [s, [-1, ...r]];
    c.forEach(([v, e], j) => { if (!(u >> j & 1) && e.includes(S[i])) { const [s2, r2] = f(i + 1, u | 1 << j); if (s2 + v > b[0]) b = [s2 + v, [j, ...r2]]; } });
    memo.set(k, b); return b;
  };
  return f(0, 0);
}
function score(c, S) {  // 점수만(배치 배열을 안 만들어 빠름). 값은 lineup()과 같은 순서로 더해 똑같다
  if (c.length > 30) throw new Error("하루에 뛰는 선수가 31명 이상");
  const m = S.length + 1, memo = new Map();
  const f = (i, u) => {
    if (i === S.length) return 0;
    const k = u * m + i;
    let b = memo.get(k);
    if (b !== undefined) return b;
    b = f(i + 1, u);
    for (let j = 0; j < c.length; j++)
      if (!(u >> j & 1) && c[j][1].includes(S[i])) { const s = f(i + 1, u | 1 << j) + c[j][0]; if (s > b) b = s; }
    memo.set(k, b); return b;
  };
  return f(0, 0);
}
const cache = new Map();  // 같은 날 같은 선수 조합의 점수는 한 번만 계산(폰에서 × 다시 계산을 빠르게)
function total(R) {
  let t = 0;
  for (let d = 0; d < D.days; d++) {
    const p = R.filter(x => x.val[d] > 0), k = d + "|" + p.map(x => x.id).sort().join();
    let s = cache.get(k);
    if (s === undefined) { s = score(p.map(x => [x.val[d], x.elig]), D.slots); cache.set(k, s); }
    t += s;
  }
  return t;
}
function sim(R) {
  let total = 0; const st = {};
  for (let d = 0; d < D.days; d++) {
    const p = R.filter(x => x.val[d] > 0), [s, a] = lineup(p.map(x => [x.val[d], x.elig]), D.slots);
    total += s; a.forEach(j => { if (j >= 0) st[p[j].id] = (st[p[j].id] || 0) + 1; });
  }
  return { total, st };
}
function spread(b, a, P) {
  let v = 0;
  for (const p of P) { const d = (a.st[p.id] || 0) - (b.st[p.id] || 0); v += Math.abs(d) * p.sd ** 2 + (d * p.se) ** 2; }
  return Math.sqrt(v);
}
function rank(A, R, F) {  // FA마다 가장 좋은 방출 상대와 순증가, 순증가 큰 순(확실도는 plan에서 고른 교체만)
  const bt = total(A), out = [];
  for (const fa of F.slice(0, 30)) {
    let best = null;
    for (const d of A.filter(p => p.ros < fa.ros).sort((x, y) => x.ros - y.ros).slice(0, 4)) {
      const L = D.limits[fa.pos];
      if (L !== undefined && R.filter(p => p !== d && p.pos === fa.pos).length >= L) continue;
      const g = total(A.filter(p => p !== d).concat([fa])) - bt;
      if (!best || g > best.g) best = { fa, d, g };
    }
    if (best && best.g > D.min) out.push(best);
  }
  return out.sort((x, y) => y.g - x.g);
}
const byId = Object.fromEntries(D.my.concat(D.fa).map(p => [p.id, p]));
function plan(ex) {
  if (!ex.size && D.init)  // 처음 화면은 리포트를 만들 때(파이썬) 계산해 둔 재편안 → 폰에서 열자마자 보인다
    return { moves: D.init.moves.map(([f, d, g, c]) => ({ fa: byId[f], d: byId[d], g, c })), base: D.init.base, after: D.init.after };
  let A = D.my.slice(), R = D.roster.slice(), F = D.fa.filter(f => !ex.has(f.id));
  const moves = [];
  for (let i = 0; i < D.n; i++) {
    const r = rank(A, R, F);
    if (!r.length) break;
    const m = r[0], after = A.filter(p => p !== m.d).concat([m.fa]);
    m.c = above(m.g, spread(sim(A), sim(after), A.concat([m.fa])));  // 선발 횟수 변화로 확실도(고른 교체만)
    moves.push(m);
    A = after; R = R.filter(p => p !== m.d).concat([m.fa]); F = F.filter(p => p !== m.fa);
  }
  return { moves, base: total(D.my), after: total(A) };
}
if (typeof document !== "undefined") {
  const ex = new Set(), $ = s => document.querySelector(s);
  const card = (l, p, c) => `<div class=${c}><small>${l}</small><b>${p.n}</b><small>${p.info}</small></div>`;
  const draw = () => {
    const P = plan(ex), M = P.moves;
    $("#pills").innerHTML = M.map((m, i) => `<li class=pill><span>${i + 1}</span>${m.fa.n}` +
      `<button type=button data-id="${m.fa.id}" aria-label="${m.fa.n} 빼기">×</button></li>`).join("");
    $("#excluded").innerHTML = ex.size ? "뺀 선수: " + D.fa.filter(f => ex.has(f.id)).map(f => f.n).join(", ") : "";
    $("#plan").innerHTML = !M.length ? (ex.size ? "<p class=lead>뺀 선수 말고는 남은 후보가 없어요. 새로고침하면 처음으로 돌아가요.</p>" : "<p class=lead>지금 로스터보다 나은 FA가 없어요. 이대로 유지하세요.</p>") :
      `<p class=now>${D.now} 할 일: <b>${M[0].d.n}</b> 방출하고 <b class=hl>${M[0].fa.n}</b> 영입</p>` +
      `<p class=lead>아래 순서대로 바꾸면 ${D.from}앞으로 ${D.days}일 선발 점수가 <b>${P.base.toFixed(0)}</b>점에서 <b>${P.after.toFixed(0)}</b>점으로 늘어요.</p>` +
      "<ol class=moves>" + M.map((m, i) => `<li><span class=when>${D.when[i]}</span><div class=pair>${card("방출", m.d, "out")}` +
        `<span class=arrow>→</span>${card("영입", m.fa, "in")}</div><span class=gain>+${m.g.toFixed(0)}` +
        `<small>확실 ${Math.round(m.c * 100)}%${m.c < .7 ? " (근소)" : ""}</small></span></li>`).join("") + "</ol>";
  };
  let busy = false;
  $("#pills").addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b || busy) return;  // 계산 중 연속으로 누르면 무시(계산이 겹쳐 폰이 멈추지 않게)
    busy = true;
    ex.add(b.dataset.id); b.closest("li").remove();  // 누르자마자 반응을 보여 주고 계산은 그다음
    $("#plan").innerHTML = "<p class=lead>다시 계산하는 중…</p>";
    setTimeout(() => {
      try { draw(); $("#pills button")?.focus(); }
      catch (err) { $("#plan").innerHTML = "<p class=lead>계산 중 문제가 생겼어요. 새로고침하면 처음으로 돌아가요.</p>"; }
      busy = false;
    }, 30);
  });
  draw();
}
if (typeof module !== "undefined") module.exports = { plan };
"""


CSS = """
:root{color-scheme:light dark;--bg:#F1F3F6;--surface:#FFF;--ink:#17202B;--muted:#5A6575;--line:#DCE1E8;
--ball:#C2500E;--on-ball:#fff;--good:#14804D;--bad:#B4382F;--spare:#2B6797;--wood:#D6A262;--plank:#CB9654;--court:#FFF4E2;--on-wood:#2B1B0C;
--num:Bahnschrift,"DIN Alternate","Arial Narrow",system-ui,sans-serif;--text:"Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#13171D;--surface:#1B212A;--ink:#E6EAF0;--muted:#9BA6B5;--line:#2D3541;
--good:#4CC48A;--bad:#E27D74;--spare:#86BAE6;--wood:#6B4A2A;--plank:#624325;--court:#E9D5B4;--on-wood:#F7E9D3;--ball:#F0853A;--on-ball:#13171D}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--text);word-break:keep-all;overflow-wrap:anywhere}
main{max-width:980px;margin:0 auto;padding:20px 16px 56px}
p{margin:0}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.logo{width:44px;height:44px}
h1{font:700 1.6rem/1.15 var(--num);margin:0}
.meta{color:var(--muted);font-size:.9rem;display:flex;gap:12px;flex-wrap:wrap}
.date{margin-left:auto;color:var(--muted);font-size:.85rem}
h2{font-size:1.1rem;margin:40px 0 10px}
.lead{margin-bottom:12px}
.lead b{font:700 1.15em var(--num)}
.note{color:var(--muted);font-size:.82rem;margin-top:8px}
.moves{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.moves li{display:grid;grid-template-columns:70px 1fr 112px;gap:12px;align-items:center;background:var(--surface);
border:1px solid var(--line);border-left:4px solid var(--ball);border-radius:8px;padding:12px 16px}
.when{font:600 .9rem var(--num);color:var(--muted)}
.now{font-size:1.15rem;font-weight:600;margin-bottom:6px}
.now .hl{color:var(--ball)}
.pair{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;min-width:0}
.pair small{display:block;color:var(--muted);font-size:.75rem}
.pair b{display:block;font-size:1.02rem}
.out b{color:var(--muted)} .in b{color:var(--ball)}
.arrow{font:700 1.3rem var(--num);color:var(--muted)}
.gain{font:700 1.5rem var(--num);color:var(--good);text-align:right}
.moves .gain small{display:block;font:400 .72rem var(--text);color:var(--muted);text-align:right}
.quota{background:var(--surface);border:1px solid var(--line);border-left:5px solid var(--good);border-radius:8px;padding:10px 14px;margin:0 0 12px}
.quota.zero{border-left-color:var(--bad)}
.q-main{margin:0;font-weight:600}
.q-main b{font:700 1.4rem var(--num);color:var(--good)}
.quota.zero .q-main b{color:var(--bad)}
.q-main span{font-weight:400;font-size:.88rem;color:var(--muted)}
.q-sub{margin:4px 0 0;font-size:.92rem}
.pills{list-style:none;padding:0;margin:0 0 6px;display:flex;flex-wrap:wrap;gap:8px}
.pill{display:flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--line);border-radius:999px;
padding:4px 5px 4px 12px;font-weight:600}
.pill span{font:700 .85rem var(--num);color:var(--ball)}
.pill button{border:0;background:var(--line);color:var(--ink);width:36px;height:36px;border-radius:50%;cursor:pointer;font-size:1rem;line-height:1}
.pill button:focus-visible{outline:2px solid var(--ball);outline-offset:2px}
#excluded:empty{display:none}
.extra{margin:10px 0 0;padding-left:20px;font-size:.9rem}
.board{display:grid;grid-template-columns:repeat(5,1fr);border:3px solid var(--court);border-radius:12px;overflow:hidden;
background:repeating-linear-gradient(90deg,var(--wood) 0 46px,var(--plank) 46px 48px);color:var(--on-wood)}
.col{padding:14px 10px 16px;min-width:0}
.col+.col{border-left:2px solid var(--court)}
.pos{font:700 1.7rem/1 var(--num)}
.count{margin:6px 0}
.count b{font:700 1.3rem var(--num)}
.to{font-weight:700;color:var(--on-wood)}
.chips{list-style:none;padding:0;margin:10px 0 0;display:flex;flex-direction:column;gap:5px}
.chip{background:#FFFCF6;color:#1E1914;border-radius:999px;padding:3px 10px;font-size:.84rem;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;box-shadow:0 1px 0 rgba(43,27,12,.35)}
.chip.main{font-weight:700}
.chip.in{background:var(--ball);color:var(--on-ball);font-weight:700}
.chip.out{text-decoration:line-through;opacity:.6}
.open{font-size:.8rem;margin-top:10px}
.tag{display:inline-block;font-size:.75rem;line-height:1.5;padding:0 8px;border-radius:999px;border:1px solid currentColor;color:var(--muted);white-space:nowrap}
.board .tag{color:var(--on-wood)}
.tag.short{background:var(--ball);border-color:var(--ball);color:var(--on-ball)}
.tag.spare{background:var(--spare);border-color:var(--spare);color:var(--bg)}
.tag.out{color:var(--bad)}
.score{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;background:var(--surface);
border:1px solid var(--line);border-radius:8px;padding:18px 16px;text-align:center}
.team{font-weight:600}
.final{font:700 2.6rem/1.1 var(--num);margin:4px 0}
.win .final{color:var(--ball)}
.vs{color:var(--muted);font-size:.8rem}
.pct{font:700 1.4rem var(--num);color:var(--ink)}
.small{color:var(--muted);font-size:.8rem}
.rows{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:8px}
.row{display:grid;grid-template-columns:1fr 70px 84px 108px;gap:10px;align-items:center;padding:10px 14px;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row.head{color:var(--muted);font-size:.75rem;padding:7px 14px}
.row.head span:not(:first-child){text-align:right}
.who{min-width:0}
.who small{display:block;color:var(--muted);font-size:.78rem}
.n{font:600 1.05rem var(--num);text-align:right}
.n small{display:block;font:400 .72rem var(--text);color:var(--muted)}
.n.gain{font-size:1.15rem}
.tags{display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end}
.std .row{grid-template-columns:28px 1fr 72px 64px}
.std .row.me{border-left:4px solid var(--ball);font-weight:700}
.method{color:var(--muted);font-size:.85rem;padding-left:20px}
@media (max-width:640px){
.board{grid-template-columns:1fr;background:var(--wood)}
.col{display:grid;grid-template-columns:58px 1fr;column-gap:10px;align-items:start}
.col+.col{border-left:0;border-top:2px solid var(--court)}
.pos{grid-row:1/5}
.chips{flex-direction:row;flex-wrap:wrap;margin-top:6px}
.open{margin-top:6px}
.moves li{grid-template-columns:1fr 92px}
.pair{grid-template-columns:1fr;gap:4px}
.arrow{display:none}
.when{grid-column:1/-1}
.row{grid-template-columns:1fr 44px 48px 64px;gap:8px;padding:10px 12px}
.tags{flex-direction:column;align-items:flex-end}
.row .tag{white-space:normal;text-align:center}
.std .row{grid-template-columns:24px 1fr 60px 56px}
.final{font-size:2rem}}
"""


def render(league_name, standings, me, box, opp, a):
    e = lambda s: html.escape(str(s))
    poss = lambda p: " ".join(x for x in BASE if x in p.eligibleSlots)
    tag = lambda text, kind="": f'<span class="tag {kind}">{e(text)}</span>'
    season = lambda avg, gp: f"{avg:.1f}점({gp}경기)" if gp else "기록 없음"
    surname = lambda p: e(p.name.split(" ", 1)[-1])
    swap = {d.playerId: f for f, d, *_ in a.moves}  # 방출 선수 → 그 자리에 영입할 선수
    ins, outs = {f.playerId for f in swap.values()}, set(swap)

    def status(p):
        s = STATUS.get(p.injuryStatus, "" if p.injuryStatus in (None, "ACTIVE", "NORMAL") else p.injuryStatus)
        return "".join(tag(x, "out") for x in (s, "웨이버" if p.waiver else "") if x)

    def who(p, detail=True):
        info = (f"<small>올 시즌 {season(p.avg_points, p.gp)}, 작년 {season(p.last_avg, p.last_gp)}, "
                f"ESPN 예상 {p.projected_avg_points:.1f}점, 출전율 {p.dur:.0%}</small>") if detail else ""
        return f"<div class=who><b>{e(p.name)}</b><small>{poss(p)} {e(p.proTeam)}</small>{info}</div>"

    # 재편안: 브라우저에서 PLAN_JS가 그린다(알약 × → 그 선수를 빼고 다시 계산)
    data = plan_data(a, info=lambda p: f"{poss(p)} {e(p.proTeam)} {status(p)}")
    names = ["이번 주", "다음 주", "그다음 주"] + [f"{n}주 뒤" for n in range(3, 12)]

    def label(w):  # w주 뒤 매치업(월요일 시작)
        d = a.acq.next_start + timedelta(days=7 * (w - 1)) if w and a.acq.next_start else None
        return names[w] + (f" {d.month}/{d.day}~" if d else "")
    data["when"] = [label(w) for w in a.week_of] if a.acq.limit > 0 else [f"{i}순위" for i in range(1, 11)]
    data["now"] = data["when"][0] if a.acq.limit > 0 else "지금"
    n0 = a.acq.next_start
    data["from"] = f"다음 매치업({n0.month}/{n0.day})부터 " if a.left == 0 and n0 else ""
    data["init"] = {"moves": [[str(f.playerId), str(d.playerId), g, c] for f, d, g, c in a.moves],  # 첫 화면은 계산 없이
                    "base": a.base.total, "after": a.new.total}
    script = "const D=" + json.dumps(data, ensure_ascii=False).replace("<", "\\u003c") + ";" + PLAN_JS  # <!--·</script 조합이 스크립트를 끊지 않게
    note = ""
    quota = ""  # 영입 한도 안내: 재편안 바로 위에 크게(남은 횟수가 0이면 빨간색)
    if a.acq.limit > 0:
        sub = (f"다 썼어요. 재편안은 다음 매치업({n0.month}/{n0.day} {'월화수목금토일'[n0.weekday()]})부터예요." if a.left == 0 and n0
               else "다 썼어요. 재편안은 다음 매치업부터예요." if a.left == 0
               else f"재편안은 매치업마다 최대 {a.acq.limit}명씩 나눴어요.")
        sub += " 매치업(월~일)마다 초기화돼요."
        if a.acq.used > a.acq.limit:
            sub += " ESPN 기록상 한도보다 많이 영입했어요. 실제 한도가 다르면 알려 주세요."
        quota = (f'<div class="quota{" zero" if a.left == 0 else ""}"><p class=q-main>이번 매치업 영입 <b>{a.left}회 남음</b> '
                 f'<span>({a.acq.used}회 사용 / 한도 {a.acq.limit}회)</span></p><p class=q-sub>{e(sub)}</p></div>')
    note += "웨이버 결과가 나온 뒤 리포트를 다시 실행하면 가장 정확해요."
    extra = f"<ul class=extra>{''.join(f'<li>{e(x)}</li>' for x in a.acts)}</ul>" if a.acts else ""

    # 포지션 구성판: 재편안이 인원을 늘리면 부족했던 것, 줄이면 과다였던 것
    gain = lambda b: b[2] if b else 0
    label = lambda r: (f"{tag('부족', 'short')} → {tag('적정', 'spare')}" if len(r.after) > len(r.before)
                       else f"{tag('과다')} → {tag('적정', 'spare')}" if len(r.after) < len(r.before) else tag("유지"))

    def chips(r):
        out = []
        for p in sorted({p.playerId: p for p in r.before + r.after}.values(), key=lambda p: p.est, reverse=True):
            kind = "in" if p.playerId in ins else "out" if p.playerId in outs else "main" if p.position == r.x else ""
            mark = {"in": "+", "out": "−"}.get(kind, "")
            out.append(f'<li class="chip {kind}">{mark}{e(p.name.split(" ", 1)[-1])}</li>')
        return "".join(out)

    board = "".join(
        f"<div class=col><p class=pos>{r.x}</p>"
        f"<p class=count><b>{len(r.before)}</b>명"
        + (f" <span class=to>→ {len(r.after)}명</span>" if len(r.after) != len(r.before) else "")
        + f"</p><p class=count>{label(r)}</p><ul class=chips>{chips(r)}</ul>"
        + (f"<p class=open>지금 보강 효과 +{gain(r.best):.0f}점 ({surname(r.best[0])})</p>" if r.best
           else "<p class=open>더 나은 FA 없음</p>") + "</div>"
        for r in a.pos)

    # 이번 주 매치업
    if box and opp and a.opp_w:
        home = box.home_team is me
        my_s, op_s = (box.home_score, box.away_score) if home else (box.away_score, box.home_score)
        mf, of = my_s + a.me_w.total, op_s + a.opp_w.total

        def side(t, s, w, f, win, sd):
            return (f"<div{' class=win' if win else ''}><p class=team>{e(t.team_name)}</p><p class=final>{f:.0f}</p>"
                    f"<p class=small>80% 범위 {f - 1.28 * sd:.0f}~{f + 1.28 * sd:.0f}<br>"
                    f"현재 {s:.0f} + 남은 예상 {w.total:.0f}<br>남은 선발 {sum(w.starts.values())}경기</p></div>")
        match = (f"<div class=score>{side(me, my_s, a.me_w, mf, mf >= of, a.me_sd)}"
                 f"<p class=vs>내 승률<br><b class=pct>{a.win:.0%}</b></p>"
                 f"{side(opp, op_s, a.opp_w, of, of > mf, a.opp_sd)}</div>"
                 "<p class=note>남은 예상 = 매치업이 끝날 때까지, 아직 시작하지 않은 경기를 매일 가장 좋은 선발로 뛴다고 볼 때 점수.</p>")
    else:
        match = "<p>이번 매치업은 상대가 없어요(BYE).</p>"

    # 내 선수
    def roster_tags(p):
        g, st = games(p, a.sps), a.base.starts[p.playerId]
        t = tag(f"방출 → {swap[p.playerId].name.split(' ', 1)[-1]}", "out") if p.playerId in swap else ""
        t += tag("IR") if p.lineupSlot == "IR" else ""
        t += tag("벤치 많음") if p.lineupSlot != "IR" and g >= 3 and st <= g / 2 else ""
        return t + status(p)
    roster = "".join(
        f"<li class=row>{who(p)}<p class=n>{p.est:.1f}<small>±{p.se:.0f}</small></p>"
        f"<p class=n>{'-' if p.lineupSlot == 'IR' else f'{a.base.starts[p.playerId]}/{games(p, a.sps)}'}</p>"
        f"<p class=tags>{roster_tags(p)}</p></li>" for p in sorted(me.roster, key=lambda p: p.est, reverse=True))
    roster_head = f"<li class='row head'><span>선수</span><span>경기당 추정</span><span>{a.days}일 선발/경기</span><span>상태</span></li>"

    # 영입 후보
    fa_rows = "".join(
        f"<li class=row>{who(f)}<p class=n>{f.est:.1f}<small>±{f.se:.0f}</small></p><p class=n>{games(f, a.sps)}</p>"
        f"<p class='n gain'>+{g:.0f}<small>{surname(d)} 대신<br>확실 {c:.0%}</small></p></li>" for f, d, g, c in a.adds[:8])
    fa_head = f"<li class='row head'><span>선수</span><span>경기당 추정</span><span>{a.days}일 경기</span><span>순증가</span></li>"
    fas = f"<ul class=rows>{fa_head}{fa_rows}</ul>" if a.adds else "<p>지금 로스터보다 나은 FA가 없어요.</p>"

    std = "".join(
        f"<li class='row{' me' if t is me else ''}'><span>{i}</span><span>{e(t.team_name)}</span>"
        f"<span class=n>{t.wins}-{t.losses}{f'-{t.ties}' if t.ties else ''}</span><span class=n>{t.points_for:.0f}</span></li>"
        for i, t in enumerate(standings, 1))
    rank = next((i for i, t in enumerate(standings, 1) if t is me), "-")

    png = base64.b64encode(icon_png()).decode()
    svg = "data:image/svg+xml," + quote(ICON_SVG)
    now = datetime.now()
    return f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{e(me.team_name)} 로스터 재편</title>
<link rel=icon href="{svg}"><link rel=icon type=image/png href="data:image/png;base64,{png}">
<link rel=apple-touch-icon href="data:image/png;base64,{png}">
<style>{CSS}</style></head><body><main>
<header class=top><img class=logo src="{svg}" alt="">
<div><h1>{e(me.team_name)}</h1><p class=meta><span>{e(league_name)}</span><span>{rank}위</span><span>{me.wins}승 {me.losses}패</span></p></div>
<p class=date>{now.month}월 {now.day}일 {now.hour}시 기준</p></header>

<h2>재편안</h2>{quota}<ul id=pills class=pills></ul><p class=note>알약은 데려올 선수 순서예요. 다른 팀이 먼저 데려간 선수는 ×를 누르면 빼고 다시 계산해요(새로고침하면 처음으로).</p><p id=excluded class=note></p><div id=plan aria-live=polite></div><p class=note>{note}</p>{extra}
<script>{script}</script>

<h2>포지션 구성</h2><div class=board>{board}</div>
<p class=note>숫자는 그 포지션을 뛸 수 있는 내 선수 수예요(재편 후 바뀌면 → 표시). 여러 포지션 선수는 칸마다 나오고, 진한 이름은 주 포지션,
+는 처음 재편안의 영입, −는 방출이에요(위에서 ×로 바꾼 재편안은 여기 반영되지 않아요). 재편안이 인원을 늘리는 포지션은 부족 → 적정,
줄이는 포지션은 과다 → 적정, 건드리지 않는 포지션은 유지예요. 보강 효과는 그 포지션 FA 중 가장 좋은 선수를 데려왔을 때 {a.days}일 선발 점수가 오르는 양이에요.
지금은 선발 {len(a.slots)}자리 중 하루 평균 {a.idle:.1f}자리가 비어요(그날 경기 있는 선수가 모자람). 그래서 실력이 비슷하면 경기 수 많은 선수가 유리하고, 영입 순위에 반영돼 있어요.</p>

<h2>이번 매치업</h2>{match}

<h2>내 선수</h2><ul class=rows>{roster_head}{roster}</ul>

<h2>영입 후보</h2>{fas}

<h2>리그 순위</h2><ul class="rows std">{std}</ul>

<h2>계산 방법</h2><ul class=method>
<li>경기당 추정: 올 시즌 평균을 작년 평균과 ESPN 예상 쪽으로 보정해요. 올 시즌 {K}경기를 뛰면 반반이라, 시즌 초 몇 경기로 튀는 숫자를 줄여 줘요.</li>
<li>출전율: ESPN 예상 출전 경기 ÷ 82(정규시즌 경기 수). ESPN 예상이 없으면 작년 출전 경기로 계산해요.</li>
<li>선발 점수: 날마다 실제 NBA 일정에서 경기가 있는 선수를 선발 {len(a.slots)}자리에 가장 높게 배치한 합이에요. OUT 선수는 복귀 예정일까지 0점.</li>
<li>순증가: 그 선수를 영입하고 방출 대상을 내보냈을 때 {a.days}일 선발 점수 차이. 방출 대상은 장기 가치(경기당 추정 × 출전율)가 더 낮은 선수만 골라요.</li>
<li>확실도: 교체로 선발 경기가 늘고 주는 선수들의 경기별 점수 흔들림(작년·올해 경기 기록)과 경기당 추정의 오차(± 숫자)로, 순증가가 0보다 클 확률을 계산해요. 70% 미만이면 근소.</li>
<li>승률: 현재 점수 + 남은 예상과, 남은 선발 경기의 흔들림으로 계산해요. 80% 범위는 예상 최종이 열 번 중 여덟 번 들어가는 구간이에요.</li>
</ul></main></body></html>"""


def login_cookies(league_id, force=False):
    """리포트 전용 크롬 프로필(browser-profile/)에서 ESPN 로그인 쿠키를 꺼낸다.
    로그인이 안 돼 있거나 만료됐으면(force) 창을 띄워 사용자가 직접 로그인하게 한다.
    보안: 쿠키 읽기 외에 페이지 내용·입력·통신을 보는 기능은 쓰지 않는다(check.py가 검사).
    Edge는 새 프로필에 Windows 계정 자동 로그인·기존 브라우저 데이터 가져오기를 해서 크롬을 쓴다.
    용량: 브라우저를 내려받지 않고 설치된 크롬을 쓴다. 없으면 설치 안내 후 종료."""
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as p:
        def launch(headless):
            try:
                return p.chromium.launch_persistent_context(HERE / "browser-profile", channel="chrome", headless=headless)
            except Error as err:
                if "is not found" in str(err):
                    raise SystemExit("크롬이 설치되어 있지 않습니다. https://www.google.com/chrome/ 에서 설치한 뒤 다시 실행하세요.")
                raise
        grab = lambda ctx: {c["name"]: c["value"] for c in ctx.cookies("https://fantasy.espn.com")}
        if not force:
            ctx = launch(True)
            c = grab(ctx)
            ctx.close()
            if "espn_s2" in c and "SWID" in c:
                return c["espn_s2"], c["SWID"]
        ctx = launch(False)
        ctx.clear_cookies()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"https://fantasy.espn.com/basketball/league?leagueId={league_id}")
        print("열린 크롬 창에서 ESPN에 로그인하세요. 로그인되면 창이 자동으로 닫힙니다.")
        try:
            while not {"espn_s2", "SWID"} <= grab(ctx).keys():
                page.wait_for_timeout(2000)
            page.wait_for_timeout(1000)  # SWID가 방문자 값에서 로그인한 회원 값으로 바뀔 시간
            c = grab(ctx)
        except Error:  # 창을 닫으면 TargetClosedError(Error의 하위)
            raise SystemExit("로그인 창이 닫혔습니다. 다시 실행하세요.")
        ctx.close()
        return c["espn_s2"], c["SWID"]


def free_agents_raw(league, size=150):
    """FA·웨이버 선수 원본(지난 시즌 기록 포함). espn-api의 free_agents()는 지난 시즌·일정을 버려서 직접 받는다"""
    filters = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]}, "limit": size,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    return league.espn_request.league_get(params={"view": "kona_player_info", "scoringPeriodId": league.current_week},
                                          headers={"x-fantasy-filter": json.dumps(filters)})["players"]


def et_now():
    return datetime.now(timezone.utc) - timedelta(hours=5)  # ponytail: 미국 동부 표준시 고정(서머타임엔 1시간 오차)


def connect(use_env=True):
    """ESPN 리그 접속 → (league, espn_s2, swid). 쿠키는 환경변수(.env·GitHub Secrets) → 크롬 프로필 순으로 쓰고,
    만료됐으면 로그인 창을 띄운다. GitHub Actions(CI)에는 창이 없어 안내 후 종료. sync_cookies.py도 이걸 쓴다."""
    from dotenv import load_dotenv
    from espn_api.basketball import League
    from espn_api.requests.espn_requests import ESPNAccessDenied
    load_dotenv(HERE / ".env")  # 없어도 됨. ESPN_S2/SWID를 넣으면 크롬 로그인 대신 그 값을 쓴다
    now = et_now()
    league_id = int(os.getenv("LEAGUE_ID", "1407913927"))  # Yushin 2026 League. 같은 리그 친구는 그대로 쓰면 자기 팀이 나온다
    year = int(os.getenv("YEAR", now.year + (now.month >= 9)))  # ESPN 시즌 연도 = 시즌이 끝나는 해
    expired = ("ESPN 쿠키가 없거나 만료됐습니다. PC에서 python sync_cookies.py 를 실행하세요"
               "(만료됐으면 크롬 로그인 창이 뜹니다). GitHub Secrets의 ESPN_S2·SWID가 자동으로 바뀝니다.")
    s2, swid = (os.getenv("ESPN_S2"), os.getenv("SWID")) if use_env else (None, None)
    if not (s2 and swid):
        if os.getenv("CI"):
            raise SystemExit(expired)
        s2, swid = login_cookies(league_id)
    try:
        return League(league_id=league_id, year=year, espn_s2=s2, swid=swid), s2, swid
    except ESPNAccessDenied:  # 로그인 만료 → 창을 띄워 다시 로그인
        if os.getenv("CI"):
            raise SystemExit(expired)
        s2, swid = login_cookies(league_id, force=True)
        return League(league_id=league_id, year=year, espn_s2=s2, swid=swid), s2, swid


def main():
    from espn_api.basketball import Player
    from espn_api.basketball.constant import POSITION_MAP
    now = et_now()
    league, s2, swid = connect()
    year = league.year
    if "POINTS" not in (league.settings.scoring_type or ""):
        raise SystemExit("포인트 방식 리그만 지원합니다(카테고리·로토 리그는 점수 계산 방식이 다름).")

    me = find_my_team(league.teams, swid)
    box = next((b for b in league.box_scores() if me in (b.home_team, b.away_team)), None)
    opp = box and (box.away_team if box.home_team is me else box.home_team)
    opp = opp if hasattr(opp, "roster") else None
    st_all = league.espn_request.league_get(params={"view": ["mSettings", "mTeam"]})
    st = st_all["settings"]
    counts = st["rosterSettings"]["lineupSlotCounts"]
    slots = tuple(POSITION_MAP[int(k)] for k, n in counts.items() if int(k) not in (12, 13) for _ in range(n))  # 12 벤치, 13 IR
    limits = {POSITION_MAP[int(k) - 1]: n for k, n in st["rosterSettings"].get("positionLimits", {}).items()
              if n > 0 and int(k) - 1 in POSITION_MAP}
    today_sp = league.scoringPeriodId
    # 매치업 = 월~일 주 단위. 플레이오프·올스타처럼 여러 주짜리면 끝나는 주의 일요일까지
    weeks = len(st["scheduleSettings"]["matchupPeriods"].get(str(league.currentMatchupPeriod), [1]))
    start = min(map(int, league.matchup_ids.get(league.currentMatchupPeriod, [today_sp])))
    week_end = today_sp + 6 - now.weekday() + 7 * max(0, weeks - 1 - (today_sp - start) // 7)

    rostered = me.roster + (opp.roster if opp else [])
    cards = league.espn_request.get_player_card([p.playerId for p in rostered], league.finalScoringPeriod, [f"00{year - 1}"])
    cards = {c["id"]: c for c in cards["players"]}
    for p in rostered:
        prepare(p, cards.get(p.playerId, {}), year, today_sp, now.date())
    fas = [prepare(Player(r, year, league.pro_schedule), r, year, today_sp, now.date()) for r in free_agents_raw(league)]
    top_fa = sorted((f for f in fas if f.injuryStatus not in ("OUT", "SUSPENSION")), key=ros, reverse=True)[:30]  # rank_adds가 보는 FA만 경기 기록(선수 카드)을 추가로 받는다
    logs = league.espn_request.get_player_card([p.playerId for p in top_fa], league.finalScoringPeriod, [f"00{year - 1}"])
    logs = {c["id"]: c for c in logs["players"]}
    for p in top_fa:
        p.cv, p.cv_n = game_cv(logs.get(p.playerId, {}))
    set_spread(rostered + fas)

    scores = ((box.home_score, box.away_score) if box.home_team is me else (box.away_score, box.home_score)) if opp else (0, 0)
    mine = next(t for t in st_all["teams"] if t["id"] == me.team_id)
    used = mine.get("transactionCounter", {}).get("matchupAcquisitionTotals", {}).get(str(league.currentMatchupPeriod), 0)
    # 한도: ESPN 설정값. 실제와 다르면 환경변수 ACQ_LIMIT(.env·GitHub 저장소 변수)으로 덮어쓴다
    acq = NS(limit=int(os.getenv("ACQ_LIMIT") or st["acquisitionSettings"].get("matchupAcquisitionLimit") or -1), used=int(used),
             next_start=now.date() + timedelta(days=week_end - today_sp + 1))
    a = analyze(me.roster, opp.roster if opp else [], fas, slots, limits, counts.get("13", 0),
                today_sp, week_end, league.finalScoringPeriod, scores, acq)
    out = HERE / "report.html"
    out.write_text(render(league.settings.name, league.standings(), me, box, opp, a), encoding="utf-8")
    print(f"저장: {out}")
    if not os.getenv("CI"):
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
