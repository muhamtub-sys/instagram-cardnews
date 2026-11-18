"""
오늘의 주요 이슈를 모아 카드뉴스(캐러셀 이미지 여러 장)로 만들고 posts.csv 에 바로 게시하도록 넣습니다.

  1) 이슈 수집: 구글 뉴스 RSS (한국, 무료·키 필요 없음)
  2) 정리: Google Gemini 무료 등급이 주요 이슈 N개를 골라 카드 문구와 본문을 씀
  3) 그리기: Pillow + 본고딕(Noto Sans KR) 으로 1080x1350 카드 이미지 생성
  4) posts.csv 에 carousel 한 줄 추가 → autopost.py 가 인스타에 올림

사용법
  python cardnews.py              # 만들고 지금 시각으로 예약
  python cardnews.py --sample     # AI·뉴스 없이 예시 문구로 카드만 그려보기 (디자인 확인용)
  python cardnews.py --no-queue   # 카드만 만들고 posts.csv 에는 넣지 않음

환경변수
  GEMINI_API_KEY   (필수) https://aistudio.google.com/apikey
  BRAND            카드에 들어갈 계정 이름 (기본 "이슈 한 장")
  HANDLE           카드 하단 계정 아이디 (예: @today.issue)
  NEWS_FEEDS       RSS 주소들, 쉼표 구분 (기본: 구글 뉴스 한국 주요뉴스)
  ISSUE_COUNT      한 번에 다룰 이슈 수 (기본 5, 최대 8)
"""

import argparse
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from PIL import Image, ImageDraw, ImageFont

from autopost import HERE, TZ, load_env, read_posts, write_posts

W, H = 1080, 1350
FONT_PATH = HERE / "fonts" / "NotoSansKR.ttf"
IMAGES = HERE / "images"
# 구글 뉴스가 GitHub 서버를 자주 막아서(429) 주요 언론사 RSS 를 함께 씀. 일부가 실패해도 나머지로 진행.
DEFAULT_FEEDS = ",".join([
    "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko",
    "https://www.yna.co.kr/rss/news.xml",                                   # 연합뉴스
    "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=01&plink=RSSREADER",  # SBS 정치
    "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=02&plink=RSSREADER",  # SBS 경제
    "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=03&plink=RSSREADER",  # SBS 사회
    "https://rss.donga.com/total.xml",                                      # 동아일보
    "https://www.khan.co.kr/rss/rssdata/total_news.xml",                    # 경향신문
    "https://www.hani.co.kr/rss/",                                          # 한겨레
    "https://www.mk.co.kr/rss/30000001/",                                   # 매일경제
    "https://fs.jtbc.co.kr/RSS/newsflash.xml",                              # JTBC
])

# 색상 (짙은 남색 배경 + 노란 포인트)
BG, BG2 = (17, 24, 39), (30, 41, 59)
FG, SUB, ACCENT = (248, 250, 252), (148, 163, 184), (250, 204, 21)


# ───────────────────────── 1. 이슈 수집 ─────────────────────────
def _get(url):
    for attempt in range(2):
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; issue-onecard-bot/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*"})
        if r.status_code == 429 and attempt == 0:
            time.sleep(5)
            continue
        r.raise_for_status()
        return r.content
    r.raise_for_status()


def fetch_news(hours=14, per_feed=15, limit=80):
    feeds = [u.strip() for u in os.environ.get("NEWS_FEEDS", DEFAULT_FEEDS).split(",") if u.strip()]
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, seen = [], set()
    for url in feeds:
        try:
            root = ET.fromstring(_get(url))
        except Exception as e:
            print(f"뉴스 가져오기 실패(건너뜀): {url} → {e}")
            continue
        channel = (root.findtext("channel/title") or "").strip()
        n = 0
        for it in root.iter("item"):
            title = html.unescape((it.findtext("title") or "").strip())
            source = (it.findtext("source") or "").strip() or channel
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            try:
                when = parsedate_to_datetime(it.findtext("pubDate"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=TZ)
            except Exception:
                when = datetime.now(timezone.utc)
            if not title or when < since or title in seen:
                continue
            seen.add(title)
            # 구글 뉴스는 관련 기사 제목들, 언론사 RSS 는 기사 요약이 들어 있음 → 맥락으로 사용
            desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
            items.append({"title": title, "source": source,
                          "related": re.sub(r"\s+", " ", desc).strip()[:300]})
            n += 1
            if n >= per_feed:
                break
        print(f"{channel or url}: {n}개")
        if len(items) >= limit:
            break
    return items


# ───────────────────────── 2. AI 정리 ─────────────────────────
SYSTEM = """당신은 인스타그램 '카드뉴스' 에디터입니다. 주어진 뉴스 제목·관련 기사 제목만 근거로 씁니다.
- 주어진 자료에 없는 숫자·인물·발언·원인은 절대 지어내지 않습니다. 모르면 쓰지 않습니다.
- 정치적으로 한쪽 편을 드는 표현, 단정적 평가, 자극적 낚시 문구는 쓰지 않습니다.
- 사망·범죄 피해자 등 개인을 특정하는 내용, 선정적인 사건은 고르지 않습니다.
- 여러 언론사가 공통으로 다룬 이슈를 우선 고르고, 분야(사회·경제·IT·국제·생활 등)를 섞습니다.
- 문장은 쉬운 존댓말, 짧게. 기사 문장을 그대로 베끼지 말고 새로 요약합니다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "cover_title": {"type": "string"},      # 표지 큰 제목, 18자 이내
        "cover_sub": {"type": "string"},        # 표지 작은 설명, 30자 이내
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},   # 사회/경제/IT/국제/생활/스포츠/연예 등 2~4자
                "headline": {"type": "string"},   # 24자 이내
                "points": {"type": "array", "items": {"type": "string"}},  # 2~3개, 각 45자 이내
                "sources": {"type": "string"},    # 근거 언론사 이름, 쉼표 구분
            },
            "required": ["category", "headline", "points", "sources"]}},
        "caption": {"type": "string"},          # 인스타 본문
    },
    "required": ["cover_title", "cover_sub", "issues", "caption"],
}


def gemini_json(prompt):
    key = os.environ["GEMINI_API_KEY"]
    models = os.environ.get("GEMINI_MODELS", os.environ.get("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.6-flash"))
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA}}
    last = None
    for model in [m.strip() for m in models.split(",") if m.strip()]:
        for attempt in range(3):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, json=body, timeout=240)
            if r.status_code in (429, 500, 503):      # 무료 등급은 몰리면 잠깐 거절 → 기다렸다 재시도
                last = f"{model}: {r.status_code}"
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 404:                  # 모델 이름이 없어졌으면 다음 모델로
                last = f"{model}: 404"
                break
            r.raise_for_status()
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    raise RuntimeError(f"Gemini 실패: {last}")


def write_cards(news, count):
    now = datetime.now(TZ)
    slot = "아침" if now.hour < 15 else "저녁"
    lines = "\n".join(f"{i+1}. [{n['source']}] {n['title']} | 관련: {n['related']}" for i, n in enumerate(news))
    prompt = f"""오늘은 {now:%Y년 %m월 %d일} {slot}입니다. 아래 뉴스 중 주요 이슈 {count}개를 골라 카드뉴스를 만들어 주세요.

[뉴스 목록]
{lines}

- cover_title: 예) "{now:%m/%d} {slot} 이슈 {count}" 처럼 짧게
- issues: 정확히 {count}개, points 는 2~3개
- caption: 첫 줄 한 문장 요약, 이어서 이슈별 한 줄씩(번호), 마지막에
  "출처: 각 언론사 보도 종합" 과 해시태그 6~10개(#이슈한장 #오늘의이슈 #뉴스요약 포함)."""
    data = gemini_json(prompt)
    data["issues"] = data["issues"][:count]
    return data


# ───────────────────────── 3. 카드 그리기 ─────────────────────────
def font(size, weight="Regular"):
    f = ImageFont.truetype(str(FONT_PATH), size)
    f.set_variation_by_name(weight)
    return f


def wrap(draw, text, f, max_w):
    """한글은 띄어쓰기 단위로, 너무 긴 단어는 글자 단위로 줄바꿈."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=f) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = ""
        for ch in word:
            if draw.textlength(cur + ch, font=f) > max_w:
                lines.append(cur)
                cur = ""
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def base(brand, page, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for y in range(H):                                 # 은은한 세로 그라데이션
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(BG[i] * (1 - t) + BG2[i] * t) for i in range(3)))
    d.rectangle([80, 96, 80 + 56, 102], fill=ACCENT)
    d.text((80, 118), brand, font=font(34, "Bold"), fill=FG)
    if total:
        d.text((W - 80, 118), f"{page}/{total}", font=font(30, "Medium"), fill=SUB, anchor="ra")
    return img, d


def footer(d, text):
    d.text((80, H - 110), text, font=font(28, "Medium"), fill=SUB)


def cover_card(data, brand, handle, total, date_text):
    img, d = base(brand, 1, total)
    y = 420
    d.text((80, y), date_text, font=font(40, "Medium"), fill=ACCENT)
    y += 90
    for line in wrap(d, data["cover_title"], font(104, "Black"), W - 160):
        d.text((80, y), line, font=font(104, "Black"), fill=FG)
        y += 130
    y += 30
    for line in wrap(d, data["cover_sub"], font(44, "Regular"), W - 160):
        d.text((80, y), line, font=font(44, "Regular"), fill=SUB)
        y += 64
    y = max(y + 60, 1000)
    for i, it in enumerate(data["issues"][:5]):
        d.text((80, y), f"{i+1}  {it['headline']}"[:30], font=font(32, "Medium"), fill=FG)
        y += 46
    footer(d, f"옆으로 넘겨보세요  →     {handle}".strip())
    return img


def issue_card(it, n, brand, handle, page, total):
    img, d = base(brand, page, total)
    y0 = 300
    # 글이 길면 글자 크기를 한 단계씩 줄여 하단 출처와 겹치지 않게 함
    for hs, ps in ((84, 48), (76, 44), (68, 40), (60, 36)):
        hf, pf = font(hs, "Bold"), font(ps, "Regular")
        head = wrap(d, it["headline"], hf, W - 160)[:3]
        points = [wrap(d, p, pf, W - 210) for p in it["points"][:3]]
        need = 110 + len(head) * hs * 1.33 + 100 + sum(len(pl) * ps * 1.5 + 40 for pl in points)
        if y0 + need <= H - 150:
            break
    y = y0
    cat = it["category"][:6]
    tw = d.textlength(cat, font=font(32, "Bold"))
    d.rounded_rectangle([80, y, 80 + tw + 48, y + 60], radius=30, fill=ACCENT)
    d.text((104, y + 30), cat, font=font(32, "Bold"), fill=BG, anchor="lm")
    d.text((W - 80, y + 30), f"{n:02d}", font=font(64, "Black"), fill=(51, 65, 85), anchor="rm")
    y += 110
    for line in head:
        d.text((80, y), line, font=hf, fill=FG)
        y += int(hs * 1.33)
    y += 40
    d.line([(80, y), (W - 80, y)], fill=(51, 65, 85), width=3)
    y += 60
    for pl in points:
        dot = int(ps * 0.55)
        d.ellipse([84, y + dot - 9, 102, y + dot + 9], fill=ACCENT)
        for line in pl:
            d.text((130, y), line, font=pf, fill=FG)
            y += int(ps * 1.5)
        y += 40
    footer(d, f"출처: {it['sources']}"[:46])
    return img


def end_card(brand, handle, total):
    img, d = base(brand, total, total)
    d.text((W // 2, 560), "오늘의 이슈 한 장, 끝!", font=font(76, "Black"), fill=FG, anchor="mm")
    d.text((W // 2, 680), "하루 두 번, 꼭 알아야 할 소식만 정리해 드려요", font=font(40), fill=SUB, anchor="mm")
    d.rounded_rectangle([240, 800, W - 240, 900], radius=50, fill=ACCENT)
    d.text((W // 2, 850), f"팔로우 {handle}".strip(), font=font(40, "Bold"), fill=BG, anchor="mm")
    d.text((W // 2, 990), "저장 · 공유하면 다음 소식도 놓치지 않아요", font=font(34), fill=SUB, anchor="mm")
    footer(d, "본 카드는 각 언론사 보도를 바탕으로 요약했습니다.")
    return img


def render(data, outdir):
    brand = os.environ.get("BRAND", "이슈 한 장")
    handle = os.environ.get("HANDLE", "@issue_onecard")
    now = datetime.now(TZ)
    date_text = f"{now:%Y.%m.%d} " + ("AM" if now.hour < 12 else "PM")
    issues = data["issues"]
    total = len(issues) + 2
    cards = [cover_card(data, brand, handle, total, date_text)]
    cards += [issue_card(it, i + 1, brand, handle, i + 2, total) for i, it in enumerate(issues)]
    cards.append(end_card(brand, handle, total))
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, c in enumerate(cards, 1):
        p = outdir / f"{i:02d}.jpg"
        c.save(p, "JPEG", quality=92)       # 인스타 API 는 JPEG 만 받음
        paths.append(p)
    return paths


SAMPLE = {
    "cover_title": "11/16 저녁 이슈 3",
    "cover_sub": "바쁜 하루, 꼭 알아야 할 소식만 모았어요",
    "issues": [
        {"category": "경제", "headline": "기준금리 동결, 연말까지 이어질까", "sources": "예시일보, 샘플경제",
         "points": ["한국은행이 이번 달 기준금리를 그대로 유지했어요", "물가와 환율 흐름을 더 지켜보겠다는 입장이에요",
                    "대출 금리는 당분간 큰 변화가 없을 전망이에요"]},
        {"category": "IT", "headline": "새 스마트폰 사전예약 시작", "sources": "샘플테크",
         "points": ["주요 통신사에서 사전예약을 받기 시작했어요", "정식 출시는 다음 주로 예정돼 있어요"]},
        {"category": "생활", "headline": "이번 주말 전국 첫 한파 예보", "sources": "예시방송",
         "points": ["주말 아침 기온이 영하로 떨어질 전망이에요", "수도관 동파와 빙판길에 주의가 필요해요",
                    "주 후반부터는 기온이 다시 오를 예정이에요"]},
    ],
}


# ───────────────────────── 4. 예약 ─────────────────────────
def raw_url(path):
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    rel = path.relative_to(HERE.parent).as_posix() if (HERE.parent / ".git").exists() else path.relative_to(HERE).as_posix()
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--no-queue", action="store_true")
    args = ap.parse_args()
    load_env()

    now = datetime.now(TZ)
    key = now.strftime("%Y-%m-%d %H:%M")
    if args.sample:
        data = SAMPLE
    else:
        count = max(2, min(int(os.environ.get("ISSUE_COUNT", "5")), 8))   # 표지+끝 포함 최대 10장
        news = fetch_news()
        if len(news) < count:
            raise SystemExit(f"최근 뉴스가 {len(news)}개뿐이라 이번 회차는 건너뜁니다.")
        data = write_cards(news, count)

    paths = render(data, IMAGES / now.strftime("%Y%m%d_%H%M"))
    print("카드 생성:", *[p.name for p in paths])
    if args.sample or args.no_queue:
        return

    rows = read_posts()
    rows.append({"publish_at": key, "type": "carousel",
                 "media_urls": "|".join(raw_url(p) for p in paths),
                 "caption": data["caption"].replace("\n", "\\n"),
                 "status": "", "post_id": "", "error": ""})
    print("posts.csv 에 예약:", key)

    # 같은 내용을 릴스(세로 영상)로도. 영상은 저장소가 커지지 않게 reels 브랜치에 최신 것만 둠.
    if os.environ.get("REELS", "on").lower() != "off":
        try:
            from reels import OUT, make_reel
            video, cover = make_reel(data, paths, OUT / f"{now:%Y%m%d_%H%M}.mp4",
                                     os.environ.get("BRAND", "이슈 한 장"))
            base = f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/reels"
            rows.append({"publish_at": key, "type": "reel",
                         "media_urls": f"{base}/{video.name}|{base}/{cover.name}",
                         "caption": data["caption"].replace("\n", "\\n"),
                         "status": "", "post_id": "", "error": ""})
            print("릴스도 예약:", key)
        except Exception as e:                    # 영상이 실패해도 카드뉴스는 그대로 올림
            print("릴스 만들기 실패(카드뉴스만 올림):", e)
    write_posts(rows)


if __name__ == "__main__":
    main()
