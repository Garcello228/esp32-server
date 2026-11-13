import uuid
import subprocess
import os
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STORAGE_DIR = Path("/tmp/esp32_videos")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MAX_DURATION = 10  # Обрезаем видео до 10 секунд, чтобы сервер не упал по памяти
TARGET_FPS = 20

def run_ffmpeg(input_path: str, output_path: str, mode: str, width: int, height: int):
    # Конвертируем в MJPEG, без звука
    base = ["ffmpeg", "-y", "-i", input_path, "-t", str(MAX_DURATION), "-an", "-c:v", "mjpeg", "-q:v", "5"]
    
    if mode == "stretch":
        vf = f"scale={width}:{height},fps={TARGET_FPS}"
    elif mode == "cover":
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={TARGET_FPS}"
    else: # contain
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={TARGET_FPS}"
        
    cmd = base + ["-vf", vf, output_path]
    subprocess.run(cmd, check=True, capture_output=True)

@app.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    mode: str = Form("stretch"),
    width: int = Form(320),
    height: int = Form(240),
):
    video_id = uuid.uuid4().hex
    input_path = STORAGE_DIR / f"{video_id}_in.mp4"
    output_path = STORAGE_DIR / f"{video_id}_out.mjpeg"

    content = await file.read()
    with open(input_path, "wb") as f:
        f.write(content)

    try:
        run_ffmpeg(str(input_path), str(output_path), mode, width, height)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"FFmpeg error: {e.stderr.decode()[:500]}")
    finally:
        input_path.unlink(missing_ok=True)

    return {"url": f"/video/{video_id}"}

@app.get("/video/{video_id}")
async def get_video(video_id: str):
    path = STORAGE_DIR / f"{video_id}_out.mjpeg"
    if not path.exists():
        raise HTTPException(404, "Video not found or expired")
    return FileResponse(path, media_type="video/x-motion-jpeg")

@app.get("/")
def root(): return {"status": "ok"}
