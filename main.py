import os
import re
import uuid
import struct
import subprocess
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="ESP32 ESV1 Video Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = Path("/tmp/esp32_videos")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MAX_INPUT_MB = 25
MAX_DURATION_SEC = 15
TARGET_FPS = 20

# Прошивка ESP32 выделяет под один сетевой JPEG-кадр 80 KiB.
MAX_ESP32_FRAME_BYTES = 80 * 1024


def run_ffmpeg(input_path: str, output_path: str, mode: str, width: int, height: int):
    base = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(MAX_DURATION_SEC),
        "-an",
        "-c:v", "mjpeg",
        "-q:v", "8",
    ]

    if mode == "stretch":
        vf = f"scale={width}:{height},fps={TARGET_FPS}"
    elif mode == "cover":
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={TARGET_FPS}"
        )
    else:
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={TARGET_FPS}"
        )

    cmd = base + ["-vf", vf, "-f", "image2pipe", output_path]
    subprocess.run(cmd, check=True, capture_output=True)


def iter_jpeg_frames(path: Path) -> Iterator[bytes]:
    """Читает склеенный MJPEG-файл по одному JPEG-кадру, не загружая весь файл в RAM."""
    buffer = bytearray()

    with path.open("rb") as source:
        while True:
            chunk = source.read(64 * 1024)
            if chunk:
                buffer.extend(chunk)

            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    # Оставляем последний байт на случай, если FF D8 разделён между чтениями.
                    keep = 1 if buffer and buffer[-1] == 0xFF else 0
                    if len(buffer) > keep:
                        if keep:
                            del buffer[:-1]
                        else:
                            buffer.clear()
                    break

                if start > 0:
                    del buffer[:start]

                end = buffer.find(b"\xff\xd9", 2)
                if end < 0:
                    break

                frame = bytes(buffer[:end + 2])
                del buffer[:end + 2]
                yield frame

            if not chunk:
                break


def inspect_frames(path: Path) -> int:
    """Проверяет кадры на совместимость с размером кольцевого буфера ESP32."""
    count = 0
    for frame in iter_jpeg_frames(path):
        if len(frame) > MAX_ESP32_FRAME_BYTES:
            raise ValueError(
                f"JPEG frame is {len(frame)} bytes; ESP32 limit is "
                f"{MAX_ESP32_FRAME_BYTES} bytes. Lower FFmpeg quality or resolution."
            )
        count += 1
    if count == 0:
        raise ValueError("FFmpeg produced no JPEG frames")
    return count


def make_esv1_stream(path: Path, start_frame: int = 0) -> Iterator[bytes]:
    # Именно этот заголовок и формат ожидает функция runStreamSession() в прошивке.
    yield b"ESV1"

    frame_index = 0
    for frame in iter_jpeg_frames(path):
        if frame_index < start_frame:
            frame_index += 1
            continue

        frame_index += 1
        yield struct.pack("<I", len(frame))
        yield frame


def safe_video_path(video_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", video_id):
        raise HTTPException(404, "Video not found")
    path = STORAGE_DIR / f"{video_id}_out.mjpeg"
    if not path.exists():
        raise HTTPException(404, "Video not found or expired")
    return path


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "ESP32 ESV1 video server",
        "format": "ESV1",
        "fps": TARGET_FPS,
        "max_input_mb": MAX_INPUT_MB,
        "max_duration_sec": MAX_DURATION_SEC,
    }


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

    content = await file.read()
    size_mb = len(content) / 1024 / 1024
    if size_mb > MAX_INPUT_MB:
        raise HTTPException(413, f"File too large: {size_mb:.1f} MB (max {MAX_INPUT_MB})")
    if not content:
        raise HTTPException(400, "Empty video file")

    print(f"UPLOAD START: {file.filename}, {size_mb:.2f} MB", flush=True)

    try:
        input_path.write_bytes(content)
        run_ffmpeg(str(input_path), str(output_path), mode, width, height)
        frame_count = inspect_frames(output_path)
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or b"").decode(errors="replace")[:1000]
        output_path.unlink(missing_ok=True)
        raise HTTPException(500, f"FFmpeg error: {error}")
    except ValueError as exc:
        output_path.unlink(missing_ok=True)
        raise HTTPException(500, str(exc))
    finally:
        input_path.unlink(missing_ok=True)

    out_size_mb = output_path.stat().st_size / 1024 / 1024
    print(
        f"UPLOAD READY: id={video_id}, frames={frame_count}, "
        f"output={out_size_mb:.2f} MB, size={width}x{height}",
        flush=True,
    )

    return {
        "id": video_id,
        "url": f"/stream/{video_id}",
        "mode": mode,
        "size": f"{width}x{height}",
        "format": "ESV1",
        "fps": TARGET_FPS,
        "frames": frame_count,
        "output_mb": round(out_size_mb, 2),
    }


@app.get("/stream/{video_id}")
def stream_video(
    video_id: str,
    fps: int = Query(default=TARGET_FPS, ge=1, le=60),
    start: float = Query(default=0, ge=0),
):
    path = safe_video_path(video_id)

    # Видеофайл подготовлен при TARGET_FPS. Прошивка запрашивает тот же FPS.
    if fps != TARGET_FPS:
        print(
            f"WARNING: ESP32 requested {fps} fps, file is {TARGET_FPS} fps",
            flush=True,
        )

    try:
        total_frames = inspect_frames(path)
    except ValueError as exc:
        raise HTTPException(500, str(exc))

    start_frame = int(start * TARGET_FPS)
    # После последнего кадра начинаем сначала: ESP32 автоматически переподключается.
    if start_frame >= total_frames:
        start_frame = 0

    print(
        f"STREAM: id={video_id}, fps={TARGET_FPS}, start_frame={start_frame}/{total_frames}",
        flush=True,
    )

    return StreamingResponse(
        make_esv1_stream(path, start_frame),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/video/{video_id}")
def delete_video(video_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", video_id):
        raise HTTPException(404, "Video not found")
    path = STORAGE_DIR / f"{video_id}_out.mjpeg"
    path.unlink(missing_ok=True)
    return {"deleted": True}
