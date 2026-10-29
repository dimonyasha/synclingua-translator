#!/usr/bin/env python3
"""Realtime-сервер перевода аудио: WebSocket /ws/translate.

Протокол (JSON поверх WebSocket):

Клиент -> сервер:
  {"type": "config", "format": "pcm16"|"wav", "sample_rate": 16000,
   "channels": 1}                         # необязательно, до первого audio
  {"type": "audio", "data": "<base64>"}   # чанк аудио
  {"type": "flush"}                       # обработать накопленное сейчас
  {"type": "reset"}                       # сбросить буфер
  (бинарный фрейм тоже принимается как сырой чанк в текущем формате)

Сервер -> клиент:
  {"type": "ready", ...}                  # после подключения
  {"type": "audio", "seq": N, "format": "wav", "data": "<base64>"}
  {"type": "no_speech", "seq": N}         # в чанке не было речи
  {"type": "error", "seq": N|null, "message": "..."}
"""
import os
import sys
import logging

# Приветственное сообщение с донатом
print("=" * 60)
print("SyncLingua Server v1.0")
print("Проект работает полностью локально. Спасибо, что вы с нами!")
print("Поддержать разработку: https://boosty.to/dimonyasha")
print("=" * 60)

log = logging.getLogger("server")

# Настраиваем кодировку
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# Указываем Piper, где искать данные eSpeak NG в собранном .exe
import shutil

if getattr(sys, 'frozen', False):
    _source_espeak = None
    _possible_paths = [
        os.path.join(sys._MEIPASS, 'piper', 'espeak-ng-data'),
        os.path.join(sys._MEIPASS, 'piper_phonemize', 'espeak-ng-data'),
        os.path.join(sys._MEIPASS, 'espeak-ng-data'),
    ]
    for _path in _possible_paths:
        if os.path.isdir(_path):
            _source_espeak = _path
            break
    
    if _source_espeak:
        # Безопасная папка без кириллицы
        _safe_dir = os.path.join(os.environ.get('PROGRAMDATA', 'C:\\ProgramData'), 'SyncLingua')
        os.makedirs(_safe_dir, exist_ok=True)
        _dest_espeak = os.path.join(_safe_dir, 'espeak-ng-data')
        
        if not os.path.isdir(_dest_espeak) or not os.listdir(_dest_espeak):
            log.info("Копирование espeak-ng-data в безопасную директорию...")
            try:
                shutil.copytree(_source_espeak, _dest_espeak)
            except FileExistsError:
                pass
            
        os.environ['ESPEAK_DATA_PATH'] = _dest_espeak
        log.info("Установлен ESPEAK_DATA_PATH: %s", _dest_espeak)

# --- ПОДКЛЮЧАЕМ DLL FFMPEG ДЛЯ TORCHCODEC ---
from paths import get_base_path
from pathlib import Path

# Папка bin лежит рядом с .exe
ffmpeg_bin_path = Path(sys.executable).parent / "bin" if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "bin"

if ffmpeg_bin_path.is_dir():
    os.add_dll_directory(str(ffmpeg_bin_path))
    os.environ["PATH"] = str(ffmpeg_bin_path) + os.pathsep + os.environ.get("PATH", "")
    print(f"[FFmpeg] DLL directory added: {ffmpeg_bin_path}")
else:
    print(f"[FFmpeg] WARNING: Path not found: {ffmpeg_bin_path}")
# --------------------------------------------

# 4. Пути к ресурсам (Argos). Должно быть ДО импорта pipeline/argostranslate
ARGOS_PACKAGES_DIR = get_base_path() / "argos_models"
os.environ["ARGOS_PACKAGES_DIR"] = str(ARGOS_PACKAGES_DIR)

# 5. Остальные импорты
import httpx
import ollama
import asyncio
import base64
import binascii
import io
import json
import tempfile
import wave
import requests

# Точечный обход SSL только для HuggingFace (чтобы антивирус не блокировал скачивание моделей)
from huggingface_hub import set_client_factory
def create_client_without_ssl_verify():
    session = requests.Session()
    session.verify = False
    return session
set_client_factory(create_client_without_ssl_verify)

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket

from pipeline import TranslationPipeline
from adapters.ollama_translation import OllamaTranslationAdapter

log = logging.getLogger("server")

TTS_ENGINE = os.getenv("TTS_ENGINE", "piper").lower()
XTTS_DEVICE = os.getenv("XTTS_DEVICE", "auto")

# ---------------------------------------------------------------- настройки
DEFAULT_SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
CHUNK_SECONDS = 3.0
MIN_SECONDS = float(os.getenv("MIN_SECONDS", "0.5"))
MAX_QUEUE = 30
ASR_MODEL = os.getenv("ASR_MODEL", "base")
ASR_DEVICE = os.getenv("ASR_DEVICE", "auto")
SAMPLE_WIDTH = 2  # PCM16 = 2 байта на сэмпл
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "translategemma:4b")

NO_SPEECH_MARKER = "Речь"

# ---------------------------------------------------------------- пайплайн
def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

VOICES_DIR = Path(os.getenv("VOICES_DIR", _app_dir() / "voices"))
XTTS_DEFAULT_VOICE = os.getenv("XTTS_VOICE", "reference.wav")

def build_tts():
    if TTS_ENGINE == "xtts":
        from adapters.xtts import XTTSAdapter
        # ОСТАВЛЕНО КАК ЕСТЬ ДЛЯ ЛИЧНОГО ТЕСТИРОВАНИЯ
        return XTTSAdapter(...)
    elif TTS_ENGINE == "f5":
        from adapters.f5_tts import F5TTSAdapter
        return F5TTSAdapter(voices_dir=VOICES_DIR, default_voice="reference_f5.wav", device=XTTS_DEVICE)
    elif TTS_ENGINE == "piper":
        from adapters import EdgeTTSAdapter 
        return EdgeTTSAdapter(voice_type="male_1")
    raise ValueError(f"Неизвестный TTS_ENGINE={TTS_ENGINE!r}")

def build_pipeline() -> TranslationPipeline:
    from adapters import FasterWhisperASR, OllamaTranslationAdapter

    return TranslationPipeline(
        asr=FasterWhisperASR(model_size=ASR_MODEL, device=ASR_DEVICE),
        translator=OllamaTranslationAdapter(source="en", target="ru", model=OLLAMA_MODEL),
        tts=build_tts(),
    )


def translate_pcm(pipeline: TranslationPipeline, pcm: bytes,
                  sample_rate: int, channels: int) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory(prefix="rt_translate_") as tmp:
        wav_in_path = Path(tmp) / "chunk.wav"
        wav_out_path = Path(tmp) / "chunk_ru.wav" # ИСПРАВЛЕНО: было mp3_path

        with wave.open(str(wav_in_path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(sample_rate)
            w.writeframes(pcm)

        # Получаем и файл, и текст
        _, translated_text = pipeline.run(wav_in_path, wav_out_path)
        
        # Проверка: если файл не создался
        if not wav_out_path.exists():
            return b"", translated_text
            
        return wav_out_path.read_bytes(), translated_text # ИСПРАВЛЕНО: читаем WAV


# ---------------------------------------------------------------- буфер
class AudioBuffer:
    """Накопитель PCM16-аудио. Чанки можно слать как сырой PCM или как WAV."""

    def __init__(self, sample_rate: int, channels: int) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._data = bytearray()

    @property
    def frame_bytes(self) -> int:
        return self.channels * SAMPLE_WIDTH

    @property
    def seconds(self) -> float:
        return (len(self._data) // self.frame_bytes) / self.sample_rate

    def append_pcm(self, raw: bytes) -> None:
        self._data += raw

    def append_wav(self, raw: bytes) -> None:
        """Чанк — самостоятельный WAV-файл: достаём из него PCM-кадры."""
        try:
            with wave.open(io.BytesIO(raw)) as w:
                if w.getsampwidth() != SAMPLE_WIDTH:
                    raise ValueError("Поддерживается только 16-bit WAV")
                params = (w.getframerate(), w.getnchannels())
                frames = w.readframes(w.getnframes())
        except wave.Error as exc:
            raise ValueError(f"Некорректный WAV: {exc}") from exc

        if not self._data:  # пустой буфер — подстраиваемся под формат чанка
            self.sample_rate, self.channels = params
        elif params != (self.sample_rate, self.channels):
            raise ValueError("Параметры WAV изменились посреди потока")
        self._data += frames

    def take(self) -> bytes:
        usable = len(self._data) - (len(self._data) % self.frame_bytes)
        chunk = bytes(self._data[:usable])
        del self._data[:usable]
        return chunk

    def clear(self) -> None:
        self._data.clear()


# ---------------------------------------------------------------- сессия
class Session:
    def __init__(self, ws: WebSocket, pipeline: TranslationPipeline,
                 executor: ThreadPoolExecutor) -> None:
        self.ws = ws
        self.pipeline = pipeline
        self.executor = executor
        self.format = "pcm16"
        self.buffer = AudioBuffer(DEFAULT_SAMPLE_RATE, 1)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.seq = 0
        self._send_lock = asyncio.Lock()
        self.chunk_seconds = CHUNK_SECONDS

    async def send(self, payload: dict) -> None:
        async with self._send_lock:
            try:
                await self.ws.send_json(payload)
            except Exception as exc:
                log.warning("Не удалось отправить клиенту: %s", exc)

    async def send_error(self, message: str, seq: int | None = None) -> None:
        log.error("Ошибка (seq=%s): %s", seq, message)
        await self.send({"type": "error", "seq": seq, "message": message})

    async def _on_set_voice(self, voice: object) -> None:
        tts = getattr(self.pipeline, "_tts", None)
        if not hasattr(tts, "change_voice"):
            await self.send_error("Текущий TTS-движок не поддерживает смену голоса")
            return
        if not isinstance(voice, str):
            await self.send_error("Не указано имя голоса")
            return
        if tts.change_voice(voice):
            await self.send({"type": "voice_changed", "voice": tts.current_voice})
        else:
            await self.send_error(f"Голос не найден: {voice}")
            
    async def handle_message(self, msg: dict) -> None:
        if msg.get("bytes") is not None:
            await self._on_audio(msg["bytes"])
            return

        text = msg.get("text")
        if text is None:
            return
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("ожидался JSON-объект")
            mtype = data.get("type")
        except ValueError as exc:
            await self.send_error(f"Некорректный JSON: {exc}")
            return

        if mtype == "audio":
            try:
                raw = base64.b64decode(data.get("data", ""), validate=True)
            except (binascii.Error, ValueError, TypeError):
                await self.send_error("Битый base64 в аудио-чанке")
                return
            await self._on_audio(raw)
        elif mtype == "set-chunk":
            try:
                val = float(data.get("value"))
                if 0.5 <= val <= 10.0:
                    self.chunk_seconds = val
                    await self.send({"type": "info", "message": f"chunk set to {val}"})
                else:
                    await self.send_error("Недопустимое значение chunk_seconds")
            except (ValueError, TypeError):
                await self.send_error("Некорректное значение chunk_seconds")
        elif mtype in ("set-voice", "set-xtts-voice"):
            await self._on_set_voice(data.get("voice"))
        elif mtype == "flush":
            await self._flush()
        elif mtype == "reset":
            self.buffer.clear()
            await self.send({"type": "info", "message": "buffer reset"})
        elif mtype == "config":
            await self._on_config(data)
        else:
            await self.send_error(f"Неизвестный тип сообщения: {mtype!r}")

    async def _on_config(self, data: dict) -> None:
        try:
            fmt = data.get("format", self.format)
            if fmt not in ("pcm16", "wav"):
                raise ValueError("format должен быть 'pcm16' или 'wav'")
            rate = int(data.get("sample_rate", self.buffer.sample_rate))
            channels = int(data.get("channels", self.buffer.channels))
            if not (8000 <= rate <= 96000) or channels not in (1, 2):
                raise ValueError("недопустимые sample_rate/channels")
        except (ValueError, TypeError) as exc:
            await self.send_error(f"Неверный config: {exc}")
            return
        self.format = fmt
        self.buffer = AudioBuffer(rate, channels)
        await self.send({"type": "info", "message": "config applied"})

    async def _on_audio(self, raw: bytes) -> None:
        if not raw:
            return
        try:
            if self.format == "wav":
                self.buffer.append_wav(raw)
            else:
                self.buffer.append_pcm(raw)
        except ValueError as exc:
            await self.send_error(str(exc))
            return

        if self.buffer.seconds >= self.chunk_seconds:
            await self._flush()

    async def _flush(self) -> None:
        if self.buffer.seconds < MIN_SECONDS:
            await self.send({"type": "info", "message": "buffer too short, skipped"})
            return
        
        pcm = self.buffer.take()
        self.seq += 1
        job = (self.seq, pcm, self.buffer.sample_rate, self.buffer.channels)
        
        if self.queue.full():
            try:
                self.queue.get_nowait() 
                self.queue.task_done()
                log.warning("Очередь переполнена, старый кусок отброшен")
            except asyncio.QueueEmpty:
                pass
                
        self.queue.put_nowait(job)

    async def worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            seq, pcm, rate, channels = await self.queue.get()
            try:
                # Блокирующий пайплайн — в отдельном потоке
                wav_bytes, text = await loop.run_in_executor(
                    self.executor, translate_pcm, self.pipeline, pcm, rate, channels)
                payload = base64.b64encode(wav_bytes).decode("ascii")
                
                # ИСПРАВЛЕНО: отправляем формат 'wav' вместо 'mp3'
                await self.send({"type": "audio", "seq": seq,
                                 "format": "wav", "data": payload, "text": text})
                log.info("seq=%d готов (%d КБ wav)", seq, len(wav_bytes) // 1024)
            except RuntimeError as exc:
                if NO_SPEECH_MARKER in str(exc):
                    await self.send({"type": "no_speech", "seq": seq})
                else:
                    log.exception("Ошибка пайплайна")
                    await self.send_error(f"Ошибка обработки: {exc}", seq)
            except Exception as exc:
                log.exception("Ошибка пайплайна")
                await self.send_error(f"Ошибка обработки: {exc}", seq)
            finally:
                self.queue.task_done()

# ---------------------------------------------------------------- Ollama
class OllamaSetupError(RuntimeError):
    """Ollama недоступна или модель не удалось подготовить."""

def _normalize(name: str) -> str:
    return name if ":" in name else f"{name}:latest"

def _installed_models(client: ollama.Client) -> set[str]:
    resp = client.list()
    names = set()
    for m in resp["models"]:
        name = m.get("model") or m.get("name")
        if name:
            names.add(_normalize(name))
    return names

def _pull_with_progress(client: ollama.Client, model: str) -> None:
    last_status: str | None = None
    last_pct = -1
    line_open = False

    for p in client.pull(model, stream=True):
        status = p.get("status") or ""
        total, done = p.get("total"), p.get("completed")

        if total and done is not None:
            pct = int(done * 100 / total)
            if status == last_status and pct == last_pct:
                continue
            sys.stdout.write(
                f"\r{status[:24]:<24} {pct:3d}%  "
                f"({done / 1e9:.2f} / {total / 1e9:.2f} ГБ)")
            sys.stdout.flush()
            line_open = True
            last_pct = pct
        elif status != last_status:
            if line_open:
                sys.stdout.write("\n")
                line_open = False
            print(status, flush=True)
            last_pct = -1
        last_status = status

    if line_open:
        sys.stdout.write("\n")
        sys.stdout.flush()

def init_ollama(model: str = OLLAMA_MODEL, host: str = OLLAMA_HOST) -> None:
    model = _normalize(model)
    try:
        installed = _installed_models(ollama.Client(host=host, timeout=5))
    except (ConnectionError, httpx.ConnectError, httpx.TimeoutException):
        raise OllamaSetupError(
            f"\nНе удалось подключиться к Ollama по адресу {host}.\n"
            "  1) Установите Ollama: https://ollama.com/download\n"
            "  2) Запустите её: приложение Ollama или команда `ollama serve`\n"
            "  3) Перезапустите этот сервер.\n"
        ) from None
    except ollama.ResponseError as exc:
        raise OllamaSetupError(f"Ollama ответила ошибкой: {exc}") from exc

    if model in installed:
        log.info("Ollama: модель %s уже установлена", model)
        return

    log.warning("Ollama: модель %s не найдена, начинаю загрузку (это может занять время)...", model)
    try:
        _pull_with_progress(ollama.Client(host=host, timeout=None), model)
    except ollama.ResponseError as exc:
        raise OllamaSetupError(
            f"Не удалось скачать модель {model}: {exc}\n"
            "Проверьте интернет-соединение и название модели.") from exc
    except (ConnectionError, httpx.HTTPError) as exc:
        raise OllamaSetupError(f"Соединение с Ollama прервалось во время загрузки: {exc}") from exc

    if model not in _installed_models(ollama.Client(host=host, timeout=5)):
        raise OllamaSetupError(f"Модель {model} не появилась в списке после загрузки.")
    log.info("Ollama: модель %s готова", model)

# ---------------------------------------------------------------- приложение
@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, init_ollama)
    except OllamaSetupError as exc:
        log.critical("%s", exc)
        raise

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
    log.info("Загрузка моделей...")
    app.state.pipeline = await loop.run_in_executor(executor, build_pipeline)
    app.state.executor = executor
    log.info("Сервер готов")
    yield
    executor.shutdown(wait=False, cancel_futures=True)

def voices_info(pipeline: TranslationPipeline) -> dict:
    tts = getattr(pipeline, "_tts", None)
    if TTS_ENGINE == "xtts" and hasattr(tts, "list_voices"):
        return {"engine": "xtts", "voices": tts.list_voices(), "voice": tts.current_voice}
    elif TTS_ENGINE == "f5" and hasattr(tts, "list_voices"):
        return {"engine": "f5", "voices": tts.list_voices(), "voice": tts.current_voice}
    return {"engine": TTS_ENGINE, "voices": [], "voice": None}
    
app = FastAPI(title="Realtime audio translator", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

@app.get("/voices")
async def get_voices() -> dict:
    return voices_info(app.state.pipeline)

@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket) -> None:
    await ws.accept()
    session = Session(ws, app.state.pipeline, app.state.executor)
    worker = asyncio.create_task(session.worker())
    log.info("Клиент подключён: %s", ws.client)

    await session.send({
        "type": "ready", "format": session.format,
        "sample_rate": session.buffer.sample_rate,
        "channels": session.buffer.channels, "chunk_seconds": session.chunk_seconds,
        **voices_info(app.state.pipeline),
    })

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            try:
                await session.handle_message(msg)
            except Exception as exc: 
                log.exception("Сбой обработки сообщения")
                await session.send_error(f"Внутренняя ошибка: {exc}")
    except Exception:
        log.exception("Соединение прервано")
    finally:
        worker.cancel()
        log.info("Клиент отключён: %s", ws.client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), workers=1)