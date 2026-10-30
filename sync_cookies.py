"""PC 크롬 프로필의 ESPN 쿠키를 GitHub 저장소 Secrets(ESPN_S2, SWID)에 넣는다: python sync_cookies.py
올리기 전에 ESPN에 접속해 쿠키가 살아 있는지 확인하고, 만료됐으면 크롬 로그인 창을 띄운다(report.connect).
GitHub 인증은 이 PC의 git 로그인(Git Credential Manager)을 쓴다. 쿠키·토큰은 출력하지 않는다.
매번 리포트 자동 실행을 '켜기'로 되돌린다(공개 저장소는 60일간 커밋이 없으면 GitHub가 끈다).
--setup: Pages(Source=GitHub Actions)도 켜고 리포트 만들기를 한 번 실행한다."""
import base64, json, os, subprocess, sys, urllib.error, urllib.request
from nacl import encoding, public
import report

REPO = "hong3007/espn-fantasy"


def github_token():
    try:
        out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True, check=True, timeout=60,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).stdout
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)["password"]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError):
        raise SystemExit("이 PC에 GitHub 로그인 정보가 없습니다. 배포 저장소에 git push를 한 번 해서 로그인한 뒤 다시 실행하세요.")


def api(token, method, path, body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "null")


def step(what, fn):
    """GitHub 요청 하나를 실행하고, 실패하면 어느 단계에서 왜 실패했는지 알려 준다"""
    try:
        return fn()
    except urllib.error.HTTPError as err:
        hint = {401: "git에 저장된 GitHub 로그인이 만료됐어요.", 403: "권한이 없어요. 다른 계정으로 로그인돼 있을 수 있어요.",
                404: f"{REPO}를 못 찾았어요. 다른 계정으로 로그인돼 있을 수 있어요."}.get(err.code, "")
        raise SystemExit(f"{what} 실패(HTTP {err.code}). {hint}")
    except urllib.error.URLError:
        raise SystemExit(f"{what} 실패: 인터넷 연결을 확인하세요.")


def main():
    _, s2, swid = report.connect(use_env=False)  # .env 값이 아니라 크롬 프로필의 (살아 있는) 쿠키
    token = github_token()
    key = step("암호화 키 받기", lambda: api(token, "GET", "/actions/secrets/public-key"))
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    for name, value in (("ESPN_S2", s2), ("SWID", swid)):
        step(f"Secrets {name} 갱신", lambda name=name, value=value: api(token, "PUT", f"/actions/secrets/{name}",
             {"encrypted_value": base64.b64encode(box.encrypt(value.encode())).decode(), "key_id": key["key_id"]}))
    print("GitHub Secrets 갱신: ESPN_S2, SWID")
    step("자동 실행 켜기", lambda: api(token, "PUT", "/actions/workflows/report.yml/enable"))
    if "--setup" in sys.argv:
        def pages():
            try:
                api(token, "POST", "/pages", {"build_type": "workflow"})
            except urllib.error.HTTPError as err:
                if err.code != 409:  # 409 = 이미 켜져 있음
                    raise
                api(token, "PUT", "/pages", {"build_type": "workflow"})
        step("Pages 켜기(Secrets는 이미 갱신됨)", pages)
        step("리포트 만들기 실행(Secrets는 이미 갱신됨)",
             lambda: api(token, "POST", "/actions/workflows/report.yml/dispatches",
                         {"ref": api(token, "GET", "")["default_branch"]}))
        print("Pages 켬, 리포트 만들기 실행 요청")


if __name__ == "__main__":
    main()
