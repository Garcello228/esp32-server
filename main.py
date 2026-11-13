import os
import uuid
import subprocess
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS — чтобы можно было тестировать из браузера
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранилище: на Render используем /tmp (бесплатно, но временно)
STORAGE_DIR = Path("/tmp/esp32_videos")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MAX_INPUT_MB = 25          # ограничение размера входного файла
MAX_DURATION_SEC = 15      # ограничение длительности (для бесплатного Render)
TARGET_FPS = 20            # FPS для MJPEG


def run_ffmpeg(input_path: str, output_path: str, mode: str, width: int, height: int):
    """
    mode: 'stretch' | 'cover' | 'contain'
    Всегда конвертируем в MJPEG — ESP32 не тянет MP4.
    Также обрезаем длительность до MAX_DURATION_SEC и убираем звук.
    """
    base = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(MAX_DURATION_SEC),   # обрезка по времени
        "-an",                          # без звука
        "-c:v", "mjpeg",
        "-q:v", "5",                    # качество JPEG (1-31, меньше = лучше)
    ]

    if mode == "stretch":
        # Растянуть на весь экран, игнорируя пропорции
        vf = f"scale={width}:{height},fps={TARGET_FPS}"

    elif mode == "cover":
        # Заполнить экран, обрезав края
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={TARGET_FPS}"
        )

    else:  # contain
        # Вписать целиком, добавив чёрные полосы
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={TARGET_FPS}"
        )

    cmd = base + ["-vf", vf, output_path]
    subprocess.run(cmd, check=True, capture_output=True)


@app.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    mode: str = Form("stretch"),
    width: int = Form(320),
    height: int = Form(240),
):
    if mode not in ("stretch", "cover", "contain"):
        raise HTTPException(400, "mode must be: stretch | cover | contain")

    if width < 16 or height < 16 or width > 800 or height > 800:
        raise HTTPException(400, "width/height out of range")

    video_id = uuid.uuid4().hex
    input_path = STORAGE_DIR / f"{video_id}_in.mp4"
    output_path = STORAGE_DIR / f"{video_id}_out.mjpeg"

    # Сохраняем входной файл
    content = await file.read()
    size_mb = len(content) / 1024 / 1024
    if size_mb > MAX_INPUT_MB:
        raise HTTPException(413, f"File too large: {size_mb:.1f} MB (max {MAX_INPUT_MB})")

    with open(input_path, "wb") as f:
        f.write(content)

    try:
        run_ffmpeg(str(input_path), str(output_path), mode, width, height)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"FFmpeg error: {e.stderr.decode()[:500]}")
    finally:
        input_path.unlink(missing_ok=True)

    out_size_mb = output_path.stat().st_size / 1024 / 1024

    return {
        "id": video_id,
        "url": f"/video/{video_id}",
        "mode": mode,
        "size": f"{width}x{height}",
        "format": "mjpeg",
        "output_mb": round(out_size_mb, 2),
    }


@app.get("/video/{video_id}")
async def get_video(video_id: str):
    path = STORAGE_DIR / f"{video_id}_out.mjpeg"
    if not path.exists():
        raise HTTPException(404, "Video not found or expired")
    return FileResponse(path, media_type="video/x-motion-jpeg")


@app.delete("/video/{video_id}")
async def delete_video(video_id: str):
    path = STORAGE_DIR / f"{video_id}_out.mjpeg"
    if path.exists():
        path.unlink()
    return {"deleted": True}


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "ESP32 video processor",
        "limits": {
            "max_input_mb": MAX_INPUT_MB,
            "max_duration_sec": MAX_DURATION_SEC,
            "target_fps": TARGET_FPS,
        },
    }