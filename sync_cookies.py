"""PC 크롬 프로필의 ESPN 쿠키를 GitHub 저장소 Secrets(ESPN_S2, SWID)에 넣는다: python sync_cookies.py
GitHub 인증은 이 PC의 git 로그인(Git Credential Manager)을 쓴다. 쿠키·토큰은 출력하지 않는다.
--setup: Pages(Source=GitHub Actions)도 켜고 리포트 만들기를 한 번 실행한다."""
import base64, json, subprocess, sys, urllib.error, urllib.request
from nacl import encoding, public
import report

REPO = "hong3007/espn-fantasy"


def github_token():
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, check=True).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)["password"]


def api(token, method, path, body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "null")


def main():
    s2, swid = report.login_cookies(1407913927)
    token = github_token()
    key = api(token, "GET", "/actions/secrets/public-key")
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    for name, value in (("ESPN_S2", s2), ("SWID", swid)):
        api(token, "PUT", f"/actions/secrets/{name}",
            {"encrypted_value": base64.b64encode(box.encrypt(value.encode())).decode(), "key_id": key["key_id"]})
    print("GitHub Secrets 갱신: ESPN_S2, SWID")
    if "--setup" in sys.argv:
        try:
            api(token, "POST", "/pages", {"build_type": "workflow"})
        except urllib.error.HTTPError as err:
            if err.code != 409:  # 409 = 이미 켜져 있음
                raise
            api(token, "PUT", "/pages", {"build_type": "workflow"})
        api(token, "POST", "/actions/workflows/report.yml/dispatches", {"ref": "main"})
        print("Pages 켬, 리포트 만들기 실행 요청")


if __name__ == "__main__":
    main()
