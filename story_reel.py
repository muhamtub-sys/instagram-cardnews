"""
주제 영상 릴스: 이슈마다 어울리는 무료 스톡 영상(Pexels) 위에 AI 음성 내레이션과 자막을 입혀 이어 붙입니다.

  장면 = 인트로 → 이슈 1..N → 마무리
  - 영상: Pixabay(또는 Pexels) 무료 API (상업적 사용 가능, 출처 표기 불필요). 못 찾으면 그 이슈의 카드 이미지로 대신
  - 대본: Gemini 가 이슈마다 쓴 narration, 검색어는 video_query
  - 음성/배경음/인코딩: reels.py 와 같음

환경변수
  PIXABAY_API_KEY  https://pixabay.com/api/docs/ (가입하면 바로 나옴)
  PEXELS_API_KEY   https://www.pexels.com/api/ (둘 중 하나만 있으면 됨)
"""

import os
import random
import re
import tempfile
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from reels import FPS, VH, VW, bgm, duration, run, tts, vertical_frame

MAX_CLIP_BYTES = 60 * 1024 * 1024


# ───────────── 스톡 영상 ─────────────
def _download(link, dest):
    with requests.get(link, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        size = 0
        with open(dest, "wb") as out:
            for chunk in resp.iter_content(1 << 20):
                size += len(chunk)
                if size > MAX_CLIP_BYTES:
                    raise RuntimeError("영상이 너무 큼")
                out.write(chunk)
    return dest


def pixabay_candidates(query):
    r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                     params={"key": os.environ["PIXABAY_API_KEY"], "q": query[:100], "video_type": "film",
                             "safesearch": "true", "per_page": 20, "order": "popular"})
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        if h.get("duration", 0) < 4:
            continue
        vids = h.get("videos", {})
        # 세로 화면으로 가운데를 잘라 쓰므로 너무 작지 않은 것: large(1920) → medium(1280) 순
        for size in ("large", "medium"):
            f = vids.get(size) or {}
            if f.get("url") and f.get("height", 0) >= 720:
                portrait = f.get("height", 0) > f.get("width", 1)
                out.append((f"pb{h['id']}", f["url"], portrait))
                break
    out.sort(key=lambda x: not x[2])        # 세로 영상 먼저
    return [(i, u) for i, u, _ in out]


def pexels_candidates(query):
    r = requests.get("https://api.pexels.com/videos/search", timeout=30,
                     headers={"Authorization": os.environ["PEXELS_API_KEY"]},
                     params={"query": query, "orientation": "portrait", "per_page": 15, "size": "medium"})
    r.raise_for_status()
    out = []
    for v in r.json().get("videos", []):
        if v.get("duration", 0) < 4:
            continue
        files = [f for f in v.get("video_files", [])
                 if f.get("width") and f.get("height") and f["height"] > f["width"] and f["height"] >= 960
                 and f.get("file_type") == "video/mp4"]
        if files:
            f = min(files, key=lambda f: abs(f["height"] - 1920))
            out.append((f"px{v['id']}", f["link"]))
    return out


def stock_available():
    return bool(os.environ.get("PIXABAY_API_KEY") or os.environ.get("PEXELS_API_KEY"))


def stock_clip(query, dest, used):
    cands = []
    if os.environ.get("PIXABAY_API_KEY"):
        cands = pixabay_candidates(query)
    if not cands and os.environ.get("PEXELS_API_KEY"):
        cands = pexels_candidates(query)
    cands = [c for c in cands if c[0] not in used]
    if not cands:
        return None
    vid, link = random.choice(cands[:5])
    _download(link, dest)
    used.add(vid)
    return dest


def find_clip(queries, dest, used):
    for q in queries:
        if not q:
            continue
        try:
            if stock_clip(q, dest, used):
                print(f"  영상: '{q}'")
                return dest
        except Exception as e:
            print(f"  영상 검색 실패 '{q}': {e}")
    return None


# ───────────── 글자 그리기 ─────────────
def _font(size, weight="Bold"):
    from cardnews import font
    return font(size, weight)


def _wrap(text, f, max_w):
    from cardnews import wrap
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    return wrap(d, text, f, max_w)


def shade(img):
    """위·아래를 어둡게 해서 글자가 잘 보이게."""
    g = Image.new("L", (1, VH))
    for y in range(VH):
        top = max(0, 1 - y / 760) * 200
        bottom = max(0, (y - VH * 0.48) / (VH * 0.52)) * 210
        g.putpixel((0, y), int(min(220, top + bottom)))
    black = Image.new("RGBA", (VW, VH), (0, 0, 0, 255))
    black.putalpha(g.resize((VW, VH)))
    img.alpha_composite(black)


def text_block(d, lines, f, y, fill=(255, 255, 255), stroke=6, gap=1.25):
    for line in lines:
        d.text((VW // 2, y), line, font=f, fill=fill, anchor="ma",
               stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += int(f.size * gap)
    return y


ACCENT, BG = (250, 204, 21), (17, 24, 39)


def overlay_issue(it, n, total, sub_text, handle):
    img = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    shade(img)
    d = ImageDraw.Draw(img)
    # 상단: 분류 배지 + 번호 + 제목
    cat = _font(40, "Bold")
    label = it.get("category", "")[:6]
    w = int(d.textlength(label, font=cat)) + 56
    d.rounded_rectangle([70, 190, 70 + w, 262], radius=36, fill=ACCENT)
    d.text((70 + w // 2, 226), label, font=cat, fill=BG, anchor="mm")
    d.text((VW - 70, 226), f"{n}/{total}", font=_font(44, "Bold"), fill=(255, 255, 255), anchor="rm")
    hf = _font(78, "Black")
    y = 300
    for line in _wrap(it["headline"], hf, VW - 140)[:3]:
        d.text((70, y), line, font=hf, fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
        y += 100
    # 자막
    if sub_text:
        sf = _font(64, "Bold")
        lines = _wrap(sub_text, sf, VW - 180)[:3]
        top = 1230 - (len(lines) - 1) * 40
        wmax = max(d.textlength(l, font=sf) for l in lines)
        d.rounded_rectangle([VW // 2 - wmax / 2 - 40, top - 28, VW // 2 + wmax / 2 + 40,
                             top + len(lines) * 80 + 12], radius=28, fill=(0, 0, 0, 150))
        text_block(d, lines, sf, top, fill=(255, 255, 255), stroke=3, gap=1.25)
    d.text((VW // 2, VH - 120), handle, font=_font(36, "Bold"), fill=(230, 230, 230), anchor="mm")
    return img


def overlay_center(title, sub, handle, sub_text=None):
    img = Image.new("RGBA", (VW, VH), (0, 0, 0, 120))
    d = ImageDraw.Draw(img)
    y = text_block(d, _wrap(title, _font(96, "Black"), VW - 140), _font(96, "Black"), 680, stroke=5)
    if sub:
        d.rectangle([VW // 2 - 50, y + 20, VW // 2 + 50, y + 28], fill=ACCENT)
        text_block(d, _wrap(sub, _font(50, "Bold"), VW - 180), _font(50, "Bold"), y + 70, stroke=4)
    if sub_text:
        sf = _font(56, "Bold")
        text_block(d, _wrap(sub_text, sf, VW - 160)[:3], sf, 1300, fill=ACCENT, stroke=5)
    d.text((VW // 2, VH - 120), handle, font=_font(36, "Bold"), fill=(230, 230, 230), anchor="mm")
    return img


# ───────────── 자막 나누기 ─────────────
def chunks(text, limit=24):
    """문장 → 화면에 한 번에 보일 짧은 덩어리 (길이를 고르게 나눔)."""
    import math
    out = []
    for sent in re.split(r"(?<=[.!?,])\s+", text.strip()):
        words = sent.split()
        if not words:
            continue
        parts = max(1, math.ceil(len(sent) / limit))
        target = len(sent) / parts
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > target * 1.15 and len(out) < 99:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            if out and len(cur) <= 6 and len(out[-1]) + len(cur) <= limit + 6:
                out[-1] += " " + cur           # 너무 짧은 꼬리는 앞에 붙임
            else:
                out.append(cur)
    return out or [text]


def timed(text, start, length):
    """글자 수에 비례해서 자막 시간 배분."""
    parts = chunks(text)
    total = sum(len(p) for p in parts) or 1
    t, out = start, []
    for p in parts:
        d = length * len(p) / total
        out.append((p, t, t + d))
        t += d
    return out


# ───────────── 장면 만들기 ─────────────
def render_scene(tmp, i, bg_clip, still, overlays, dur):
    """배경(영상 또는 정지 이미지) + 시간별 글자 레이어 → 무음 장면 영상."""
    seg = tmp / f"scene{i}.mp4"
    if bg_clip:
        inputs = ["-stream_loop", "-1", "-i", str(bg_clip)]
        base = (f"[0:v]scale={VW}:{VH}:force_original_aspect_ratio=increase,crop={VW}:{VH},"
                f"fps={FPS},setsar=1[b0]")
    else:
        inputs = ["-loop", "1", "-framerate", str(FPS), "-i", str(still)]
        frames = int(dur * FPS)
        base = (f"[0:v]scale=2160:-1,zoompan=z='1+0.05*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={VW}x{VH}:fps={FPS},setsar=1[b0]")
    chain, last = [base], "b0"
    for k, (png, a, b) in enumerate(overlays):
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(png)]
        chain.append(f"[{last}][{k + 1}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[b{k + 1}]")
        last = f"b{k + 1}"
    chain.append(f"[{last}]format=yuv420p[v]")
    run("ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", "[v]",
        "-t", f"{dur:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(seg))
    return seg


def make_story_reel(data, card_paths, out_path, brand="이슈 한 장"):
    handle = os.environ.get("HANDLE", "@issue_onecard")
    issues = data["issues"]
    n = len(issues)
    from autopost import TZ
    slot = "오늘 아침" if datetime.now(TZ).hour < 15 else "오늘 저녁"
    scenes = [{"kind": "intro", "query": ["city timelapse", "newspaper reading"],
               "text": f"{slot} 꼭 알아야 할 이슈 {n}가지, 1분 만에 정리해 드릴게요."}]
    for i, it in enumerate(issues):
        text = it.get("narration") or f"{it['headline']}. " + " ".join(it.get("points", []))
        scenes.append({"kind": "issue", "it": it, "n": i + 1, "text": text,
                       "query": [it.get("video_query"), " ".join((it.get("video_query") or "").split()[:2]), "abstract background"]})
    scenes.append({"kind": "outro", "query": ["phone scrolling social media", "city night lights"],
                   "text": f"{brand} 소식은 여기까지예요. 팔로우하고 다음 소식도 받아보세요."})

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    used = set()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        segs, voices, durs = [], [], []
        for i, sc in enumerate(scenes):
            v = tmp / f"v{i}.mp3"
            tts(sc["text"], v)                      # 이 방식은 음성이 핵심이라 실패하면 카드 방식으로 넘어감
            vd = duration(v)
            dur = vd + 0.8
            clip = find_clip(sc["query"], tmp / f"clip{i}.mp4", used)
            card = card_paths[min(i, len(card_paths) - 1)]
            still = tmp / f"still{i}.png"
            vertical_frame(card).save(still)

            if sc["kind"] == "issue":
                subs = timed(sc["text"], 0.3, vd)
                overlays = []
                for k, (txt, t0, t1) in enumerate(subs):
                    png = tmp / f"o{i}_{k}.png"
                    overlay_issue(sc["it"], sc["n"], n, txt, handle).save(png)
                    # 첫 자막은 장면 시작부터, 마지막 자막은 장면 끝까지 보이게
                    overlays.append((png, 0 if k == 0 else t0, dur + 1 if k == len(subs) - 1 else t1))
            else:
                png = tmp / f"o{i}.png"
                if sc["kind"] == "intro":
                    img = overlay_center(data.get("cover_title", ""), data.get("cover_sub", ""), handle)
                else:
                    img = overlay_center(f"{brand}", "팔로우하고 다음 소식도 받아보세요", handle)
                img.save(png)
                overlays = [(png, 0, dur + 1)]
            segs.append(render_scene(tmp, i, clip, still, overlays, dur))
            voices.append(v)
            durs.append(dur)
            print(f"장면 {i + 1}/{len(scenes)} {dur:.1f}초")

        (tmp / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs))
        video = tmp / "video.mp4"
        run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-c", "copy", str(video))
        total = sum(durs)

        bg = tmp / "bgm.wav"
        bgm(total, bg)
        vol = float(os.environ.get("REEL_BGM_VOL", "0.10"))
        inputs, filters, labels = ["-i", str(video), "-i", str(bg)], [f"[1:a]volume={vol}[bg]"], ["[bg]"]
        start = 0.0
        for k, (v, d) in enumerate(zip(voices, durs)):
            ms = int((start + 0.3) * 1000)
            inputs += ["-i", str(v)]
            filters.append(f"[{k + 2}:a]aresample=44100,pan=stereo|c0=c0|c1=c0,adelay={ms}|{ms}[a{k}]")
            labels.append(f"[a{k}]")
            start += d
        filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,"
                       f"afade=t=out:st={max(0, total - 1.2):.2f}:d=1.2[aout]")
        run("ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-t", f"{total:.2f}", "-movflags", "+faststart", str(out_path))

        cover = out_path.with_suffix(".jpg")
        run("ffmpeg", "-y", "-ss", "0.5", "-i", str(out_path), "-frames:v", "1", "-q:v", "3", str(cover))
    print(f"주제 영상 릴스 생성: {out_path.name} ({total:.1f}초)")
    return out_path, cover
