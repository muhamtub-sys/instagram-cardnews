"""
인스타그램 자동(예약) 포스팅 스크립트 — 공식 Instagram Graph API 사용

사용법
  python autopost.py            # posts.csv 에서 게시 시각이 지난 글을 올림
  python autopost.py --dry-run  # 실제로 올리지 않고 무엇을 올릴지만 출력
  python autopost.py --check    # 토큰/계정 연결과 24시간 게시 한도 확인

필요한 환경변수 (.env 파일 또는 시스템 환경변수)
  IG_USER_ID       인스타그램 프로페셔널(비즈니스/크리에이터) 계정 ID
  IG_ACCESS_TOKEN  장기 액세스 토큰 (60일 유효, refresh 가능)
  IG_API_HOST      graph.instagram.com (인스타그램 로그인 방식, 기본값)
                   또는 graph.facebook.com (페이스북 페이지 연결 방식)
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).parent
POSTS_CSV = HERE / "posts.csv"
TZ = ZoneInfo(os.environ.get("IG_TIMEZONE", "Asia/Seoul"))
API_VERSION = os.environ.get("IG_API_VERSION", "v21.0")
FIELDS = ["publish_at", "type", "media_urls", "caption", "status", "post_id", "error"]


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class IG:
    def __init__(self):
        self.user_id = os.environ["IG_USER_ID"]
        self.token = os.environ["IG_ACCESS_TOKEN"]
        host = os.environ.get("IG_API_HOST", "graph.instagram.com")
        self.base = f"https://{host}/{API_VERSION}"

    def _call(self, method, path, **params):
        params["access_token"] = self.token
        r = requests.request(method, f"{self.base}/{path}", params=params, timeout=60)
        data = r.json()
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(data.get("error", data))
        return data

    def me(self):
        return self._call("GET", self.user_id, fields="id,username")

    def publishing_limit(self):
        return self._call("GET", f"{self.user_id}/content_publishing_limit",
                          fields="quota_usage,config")

    def _container(self, **params):
        return self._call("POST", f"{self.user_id}/media", **params)["id"]

    def _wait_ready(self, container_id, timeout=300):
        """동영상(릴스)은 서버 처리 시간이 있어 FINISHED 가 될 때까지 기다림."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self._call("GET", container_id, fields="status_code")["status_code"]
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"미디어 처리 실패: {status}")
            time.sleep(5)
        raise TimeoutError("미디어 처리 시간이 초과되었습니다")

    def _publish(self, container_id):
        self._wait_ready(container_id)
        return self._call("POST", f"{self.user_id}/media_publish",
                          creation_id=container_id)["id"]

    def post_image(self, url, caption):
        return self._publish(self._container(image_url=url, caption=caption))

    def post_reel(self, url, caption, cover_url=None):
        params = {"media_type": "REELS", "video_url": url, "caption": caption,
                  "share_to_feed": "true"}
        if cover_url:
            params["cover_url"] = cover_url
        cid = self._container(**params)
        self._wait_ready(cid, timeout=900)      # 영상은 처리에 몇 분 걸릴 수 있음
        return self._publish(cid)

    def post_carousel(self, urls, caption):
        children = []
        for u in urls:
            is_video = u.lower().split("?")[0].endswith((".mp4", ".mov"))
            params = {"is_carousel_item": "true"}
            params.update({"media_type": "VIDEO", "video_url": u} if is_video
                          else {"image_url": u})
            cid = self._container(**params)
            self._wait_ready(cid)
            children.append(cid)
        return self._publish(self._container(media_type="CAROUSEL",
                                             children=",".join(children),
                                             caption=caption))


def read_posts():
    with POSTS_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            r.setdefault(k, "")
    return rows


def write_posts(rows):
    tmp = POSTS_CSV.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(POSTS_CSV)


def due(row, now):
    if row["status"].strip().lower() in ("posted", "skip", "error"):
        return False
    when = datetime.strptime(row["publish_at"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return when <= now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    load_env()

    if args.check:
        if not os.environ.get("IG_USER_ID"):
            # 시스템 사용자 토큰(EAA…)이면 연결된 인스타 계정 ID 를 찾아 보여줌
            os.environ["IG_USER_ID"] = "me"
            ig = IG()
            for path, fields in (("me/accounts", "name,instagram_business_account{id,username}"),
                                 ("me", "id,name")):
                try:
                    print(path, ig._call("GET", path, fields=fields))
                except Exception as e:
                    print(path, "실패:", e)
            return
        ig = IG()
        print("계정:", ig.me())
        print("게시 한도:", ig.publishing_limit())
        return

    rows = read_posts()
    now = datetime.now(TZ)
    todo = [r for r in rows if due(r, now)]
    if not todo:
        print("올릴 글이 없습니다.")
        return

    ig = None if args.dry_run else IG()
    for r in todo:
        kind = (r["type"] or "image").strip().lower()
        urls = [u.strip() for u in r["media_urls"].split("|") if u.strip()]
        caption = r["caption"].replace("\\n", "\n")
        print(f"[{r['publish_at']}] {kind} {len(urls)}개 미디어 → ", end="")
        if args.dry_run:
            print("(dry-run)")
            continue
        try:
            if kind == "image":
                pid = ig.post_image(urls[0], caption)
            elif kind == "reel":
                pid = ig.post_reel(urls[0], caption, urls[1] if len(urls) > 1 else None)
            elif kind == "carousel":
                pid = ig.post_carousel(urls, caption)
            else:
                raise ValueError(f"알 수 없는 type: {kind}")
            r.update(status="posted", post_id=pid, error="")
            print("완료", pid)
        except Exception as e:  # 한 글이 실패해도 나머지는 계속
            r.update(status="error", error=str(e)[:300])
            print("실패", e)
        write_posts(rows)


if __name__ == "__main__":
    sys.exit(main())
