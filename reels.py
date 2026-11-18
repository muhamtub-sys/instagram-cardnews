"""
인스타 릴스(세로 영상)를 만듭니다. 무료 도구만 사용.

기본(REEL_STYLE=story): 이슈별 스톡 영상 + AI 음성 + 자막 → story_reel.py
REEL_STYLE=cards: 아래 설명처럼 카드뉴스 이미지를 슬라이드 영상으로

  - 화면: 1080x1920(9:16), 카드를 가운데 두고 위아래는 같은 카드를 흐리게 깔아 채움, 천천히 확대되는 움직임
  - 음성: edge-tts (무료, 키 필요 없음) 로 카드 내용을 읽어줌. 실패하면 음성 없이 진행
  - 배경음: music/ 폴더에 mp3·m4a 를 넣으면 그중 하나를 무작위로, 없으면 ffmpeg 로 만든 잔잔한 음
  - 인코딩: H.264 + AAC, 30fps (인스타 릴스 권장 규격)

사용법
  python reels.py --sample     # 예시 카드로 영상 만들어 보기 → reels_out/sample.mp4

환경변수
  REEL_VOICE   edge-tts 목소리 (기본 ko-KR-SunHiNeural, 남성은 ko-KR-InJoonNeural)
  REEL_RATE    말하기 속도 (기본 +15%)
  REEL_TTS     off 로 두면 음성 없이 배경음만
  REEL_BGM_VOL 배경음 크기 0~1 (기본 음성 있을 때 0.12, 없을 때 0.35)
"""

import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

HERE = Path(__file__).parent
OUT = HERE / "reels_out"
MUSIC = HERE / "music"
VW, VH, FPS = 1080, 1920, 30


def run(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


# ───────────── 화면 ─────────────
def vertical_frame(card_path):
    """4:5 카드를 9:16 화면 가운데에 놓고, 빈 곳은 흐린 배경으로."""
    card = Image.open(card_path).convert("RGB")
    bg = card.resize((VH * card.width // card.height, VH))
    left = (bg.width - VW) // 2
    bg = bg.crop((left, 0, left + VW, VH)).filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    card = card.resize((VW, VW * card.height // card.width))
    bg.paste(card, (0, (VH - card.height) // 2))
    return bg


# ───────────── 내레이션 ─────────────
def scripts(data, handle_name):
    out = [f"{data.get('cover_title', '')}. {data.get('cover_sub', '')}"]
    for i, it in enumerate(data["issues"], 1):
        out.append(f"{i}번. {it['headline']}. " + " ".join(it.get("points", [])))
    out.append(f"오늘 {handle_name} 소식은 여기까지예요. 팔로우하고 다음 소식도 받아보세요.")
    return out


def tts(text, path):
    import edge_tts
    voice = os.environ.get("REEL_VOICE", "ko-KR-SunHiNeural")
    rate = os.environ.get("REEL_RATE", "+15%")

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
    asyncio.run(go())


# ───────────── 배경음 ─────────────
def bgm(total, path):
    tracks = sorted([p for p in MUSIC.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg")]) if MUSIC.exists() else []
    if tracks:
        run("ffmpeg", "-y", "-stream_loop", "-1", "-i", str(random.choice(tracks)), "-t", f"{total:.2f}",
            "-ac", "2", "-ar", "44100", str(path))
        return
    # 저작권 걱정 없는 직접 만든 음: 4마디 화음(C-Am-F-G)을 부드럽게 반복
    chords = [(261.63, 329.63, 392.00), (220.00, 261.63, 329.63),
              (174.61, 220.00, 261.63), (196.00, 246.94, 293.66)]
    bar = 4.0
    parts = []
    for i, ch in enumerate(chords):
        cond = f"between(mod(t,{bar*4}),{i*bar},{(i+1)*bar})"
        tone = "+".join(f"sin(2*PI*{f}*t)+0.3*sin(2*PI*{f*2}*t)" for f in ch)
        parts.append(f"{cond}*({tone})")
    env = f"(0.6+0.4*sin(PI*mod(t,{bar})/{bar}))"
    expr = f"0.05*{env}*({'+'.join(parts)})"
    run("ffmpeg", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={total:.2f}",
        "-af", "lowpass=f=1800,aecho=0.8:0.7:120:0.25,pan=stereo|c0=c0|c1=c0", str(path))


# ───────────── 합치기 ─────────────
def make_reel(data, card_paths, out_path, handle_name="이슈 한 장"):
    """카드 이미지 + 문구 → 릴스 mp4. 표지 이미지(9:16 jpg) 경로도 함께 돌려줌."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg 가 없습니다")
    # 기본은 주제 영상 방식(스톡 영상+음성+자막). 키가 없거나 실패하면 예외 → 이번 회차 릴스는 건너뜀.
    # REEL_STYLE=cards 로 두면 아래 카드 슬라이드 방식.
    if os.environ.get("REEL_STYLE", "story").lower() != "cards":
        from story_reel import make_story_reel, stock_available
        if not stock_available():
            raise RuntimeError("PIXABAY_API_KEY(또는 PEXELS_API_KEY)가 없어 이번 회차 릴스는 건너뜁니다")
        return make_story_reel(data, card_paths, out_path, handle_name)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    texts = scripts(data, handle_name)
    use_tts = os.environ.get("REEL_TTS", "on").lower() != "off"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        voices, durs = [], []
        for i, (card, text) in enumerate(zip(card_paths, texts)):
            vertical_frame(card).save(tmp / f"f{i}.png")
            v = None
            if use_tts:
                try:
                    v = tmp / f"v{i}.mp3"
                    tts(text, v)
                except Exception as e:
                    print("음성 만들기 실패 → 음성 없이 진행:", e)
                    use_tts, v = False, None
            voices.append(v)
            durs.append(max(3.0, duration(v) + 0.7) if v else (2.5 if i == 0 else 4.0))
        if not use_tts:
            voices = [None] * len(voices)
            durs = [2.5 if i == 0 else 4.0 for i in range(len(durs))]

        # 장면별 영상 (천천히 4% 확대)
        segs = []
        for i, d in enumerate(durs):
            seg = tmp / f"s{i}.mp4"
            frames = int(d * FPS)
            run("ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(tmp / f"f{i}.png"),
                "-vf", f"scale=2160:-1,zoompan=z='1+0.04*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                       f":d={frames}:s={VW}x{VH}:fps={FPS},format=yuv420p",
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", str(seg))
            segs.append(seg)
        (tmp / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs))
        video = tmp / "video.mp4"
        run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-c", "copy", str(video))
        total = sum(durs)

        # 음성: 장면 시작에 맞춰 이어 붙임
        bg = tmp / "bgm.wav"
        bgm(total, bg)
        vol = float(os.environ.get("REEL_BGM_VOL", "0.12" if use_tts else "0.35"))
        inputs, filters, labels = ["-i", str(video), "-i", str(bg)], [f"[1:a]volume={vol}[bg]"], ["[bg]"]
        start, n = 0.0, 2
        for v, d in zip(voices, durs):
            if v:
                inputs += ["-i", str(v)]
                ms = int((start + 0.3) * 1000)
                filters.append(f"[{n}:a]aresample=44100,pan=stereo|c0=c0|c1=c0,adelay={ms}|{ms}[a{n}]")
                labels.append(f"[a{n}]")
                n += 1
            start += d
        filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,"
                       f"afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[aout]")
        run("ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-t", f"{total:.2f}", "-movflags", "+faststart", str(out_path))

        cover = out_path.with_suffix(".jpg")
        Image.open(tmp / "f0.png").convert("RGB").save(cover, "JPEG", quality=90)
    print(f"릴스 생성: {out_path.name} ({total:.1f}초, 음성 {'있음' if use_tts else '없음'})")
    return out_path, cover


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="예시 카드로 만들어 보기")
    args = ap.parse_args()
    from story_reel import stock_available
    if not stock_available():
        os.environ.setdefault("REEL_STYLE", "cards")
    from cardnews import SAMPLE, render
    paths = render(SAMPLE, OUT / "sample_cards")
    make_reel(SAMPLE, paths, OUT / "sample.mp4")
