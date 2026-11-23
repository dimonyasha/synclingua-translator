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
import time
from urllib.parse import urlparse
from adapters.openai_translation import OpenAITranslationAdapter, TranslationError
from adapters.fallback_translation import FallbackTranslationAdapter
import os
import sys
import logging

# Приветственное сообщение с донатом
print("=" * 60)
print("SyncLingua Server v1.3.0")
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

# Отключаем проверку SSL для HuggingFace (чтобы антивирус не мешал скачиванию моделей)
os.environ["HF_HUB_DISABLE_SSL_VERIFY"] = "1"

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
MAX_QUEUE = 3
ASR_MODEL = os.getenv("ASR_MODEL", "small")
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

def build_shared_models():
    from adapters import FasterWhisperASR
    # Загружаем только тяжелые модели (ASR и TTS), которые можно шарить
    asr = FasterWhisperASR(model_size=ASR_MODEL, device=ASR_DEVICE)
    tts = build_tts()
    return asr, tts


def translate_pcm(pipeline, pcm, sample_rate, channels, subtitle_only: bool) -> tuple[bytes | None, str]:
    with tempfile.TemporaryDirectory(prefix="rt_translate_") as tmp:
        wav_in_path = Path(tmp) / "chunk.wav"
        wav_out_path = Path(tmp) / "chunk_ru.wav"

        with wave.open(str(wav_in_path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(sample_rate)
            w.writeframes(pcm)

        # Получаем и файл, и текст
        _, translated_text = pipeline.run(wav_in_path, wav_out_path, subtitle_only=subtitle_only) # <--- Убрана лишняя скобка
        
        # Если режим только субтитров, файл не создается
        if subtitle_only:
            return None, translated_text
            
        if not wav_out_path.exists():
            return b"", translated_text
            
        return wav_out_path.read_bytes(), translated_text

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
                 executor: ThreadPoolExecutor, warmup_executor: ThreadPoolExecutor) -> None:
        self.warmup_executor = warmup_executor # <-- ДОБАВИТЬ
        self._warmup_task = None # <-- ДОБАВИТЬ
        self._tr_disabled = False        
        self.ws = ws
        self.pipeline = pipeline
        self.executor = executor
        self.format = "pcm16"
        self.buffer = AudioBuffer(DEFAULT_SAMPLE_RATE, 1)
        self.asr_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self.trans_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self.tts_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self.seq = 0
        self._send_lock = asyncio.Lock()
        self.chunk_seconds = CHUNK_SECONDS
        self.subtitle_only = False

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
    
    async def _on_set_translation(self, data: dict) -> None:
        verify_ssl = data.get("verify_ssl", True)
        auto_fallback = data.get("auto_fallback", False)
        
        if self._warmup_task and not self._warmup_task.done():
            self._warmup_task.cancel()
            self._warmup_task = None

        try:
            mode = data.get("mode", "local")
            old_translator = self.pipeline._translator
            self._tr_disabled = False

            if mode == "cloud":
                base_url = data.get("base_url", "").strip()
                api_key = data.get("api_key", "").strip()
                model = data.get("model", "").strip()
                
                if not api_key or not model or not base_url:
                    raise ValueError("Для облака укажите URL, ключ и модель")
                
                u = urlparse(base_url)
                is_local = u.hostname in ("127.0.0.1", "localhost", "::1")
                if not (u.scheme == "https" or (u.scheme == "http" and is_local)):
                    raise ValueError("Для облака нужен https:// (безопасное соединение)")
                
                cloud = OpenAITranslationAdapter(
                    source="en", target="ru", model=model, base_url=base_url, api_key=api_key,
                    verify_ssl=verify_ssl
                )
                
                if auto_fallback:
                    local = OpenAITranslationAdapter(
                        source="en", target="ru", model=OLLAMA_MODEL, 
                        base_url="http://127.0.0.1:11434/v1", api_key="ollama"
                    )
                    self.pipeline._translator = FallbackTranslationAdapter(cloud, local)
                    log.info("Режим: ОБЛАКО + FALLBACK. Модель=%s, URL=%s", model, base_url)
                                # Запускаем фоновый прогрев для облака
                if not self._warmup_task or self._warmup_task.done():
                    async def _do_warmup():
                        loop = asyncio.get_running_loop()
                        try:
                            await loop.run_in_executor(self.warmup_executor, self.pipeline._translator.warmup)
                        except Exception as e:
                            log.warning("Ошибка в задаче прогрева: %s", e)
    
                    self._warmup_task = asyncio.create_task(_do_warmup())
                    log.info("Задача прогрева облака запущена.")
    
                else:
                    self.pipeline._translator = cloud
                    log.info("Режим: ОБЛАКО. Модель=%s, URL=%s", model, base_url)
            else:
                self.pipeline._translator = OpenAITranslationAdapter(
                    source="en", target="ru", model=OLLAMA_MODEL, 
                    base_url="http://127.0.0.1:11434/v1", api_key="ollama"
                )
                log.info("Режим: ЛОКАЛЬНО. Модель=%s, URL=http://127.0.0.1:11434/v1", OLLAMA_MODEL)
                
                if not self._warmup_task or self._warmup_task.done():
                    async def _do_warmup():
                        loop = asyncio.get_running_loop()
                        try:
                            await loop.run_in_executor(self.warmup_executor, self.pipeline._translator.warmup)
                        except Exception as e:
                            log.warning("Ошибка в задаче прогрева: %s", e)
                    
                    self._warmup_task = asyncio.create_task(_do_warmup())
                    self._warmup_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                    log.info("Задача прогрева запущена в фоновом режиме.")
            
            if hasattr(old_translator, "close"):
                # Даем 10 секунд, чтобы текущие запросы завершились, и только потом закрываем
                asyncio.get_running_loop().call_later(10.0, old_translator.close)
                
            await self.send({"type": "info", "message": "Настройки перевода применены"})
        except Exception as e:
            await self.send_error(f"Ошибка смены перевода: {e}")
            
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
        elif mtype == "set-subtitle-only":
            self.subtitle_only = data.get("value", False)
            log.info("Режим 'Только субтитры': %s", self.subtitle_only)
            await self.send({"type": "info", "message": f"Subtitle only: {self.subtitle_only}"})
        elif mtype in ("set-voice", "set-xtts-voice"):
            await self._on_set_voice(data.get("voice"))
        elif mtype == "flush":
            await self._flush()
        elif mtype == "reset":
            self.buffer.clear()
            await self.send({"type": "info", "message": "buffer reset"})
        elif mtype == "config":
            await self._on_config(data)
        elif mtype == "set-translation":     # <--- ДОБАВИТЬ ВОТ ЭТИ ДВЕ СТРОКИ
            await self._on_set_translation(data)
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
        
        if self.asr_queue.full():
            try:
                self.asr_queue.get_nowait() 
                self.asr_queue.task_done()
                log.warning("Очередь ASR переполнена, старый кусок отброшен")
            except asyncio.QueueEmpty:
                pass
        
        self.asr_queue.put_nowait(job)

    # async def worker(self) -> None:
        # loop = asyncio.get_running_loop()
        # last_error = None
        # last_error_time = 0.0
        
        # while True:
            # seq, pcm, rate, channels = await self.queue.get()
            # try:
                # if self._tr_disabled:
                    # continue
                    
                # wav_bytes, text = await loop.run_in_executor(
                    # self.executor, translate_pcm, self.pipeline, pcm, rate, channels, self.subtitle_only)

                # # Проверяем, не хочет ли переводчик сообщить что-то клиенту
                # pending = self.pipeline.take_pending_message()
                # if pending:
                    # await self.send(pending)
                    
                # # ПРОВЕРКА НА РЕЖИМ СУБТИТРОВ
                # # Если wav_bytes None (режим субтитров) ИЛИ пустой (короткая фраза) - шлем текст
                # if wav_bytes is None or wav_bytes == b"": 
                    # await self.send({"type": "subtitle", "seq": seq, "text": text})
                # elif wav_bytes:
                    # payload = base64.b64encode(wav_bytes).decode("ascii")
                    # await self.send({"type": "audio", "seq": seq, "format": "wav", "data": payload, "text": text})
                # else:
                    # await self.send({"type": "no_speech", "seq": seq})
                    
            # except RuntimeError as exc:
                # if NO_SPEECH_MARKER in str(exc):
                    # await self.send({"type": "no_speech", "seq": seq})
                # else:
                    # await self.send_error(f"Ошибка обработки: {exc}", seq)
            # except TranslationError as exc:
                # log.warning("Ошибка перевода: %s", exc)
                # if exc.fatal:
                    # self._tr_disabled = True
                    # await self.send({"type": "error", "seq": seq, "message": str(exc), "fatal": True})
                # else:
                    # now = time.monotonic()
                    # if str(exc) != last_error or (now - last_error_time) > 15:
                        # last_error = str(exc)
                        # last_error_time = now
                        # await self.send({"type": "error", "seq": seq, "message": str(exc), "fatal": False})
                    # else:
                        # await self.send({"type": "no_speech", "seq": seq})
            # except Exception as exc:
                # log.exception("Ошибка пайплайна")
                # await self.send_error(f"Ошибка обработки: {exc}", seq)
            # finally:
                # self.queue.task_done()

    async def asr_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            seq, pcm, rate, channels = await self.asr_queue.get()
            try:
                with tempfile.TemporaryDirectory(prefix="rt_asr_") as tmp:
                    wav_path = Path(tmp) / "chunk.wav"
                    with wave.open(str(wav_path), "wb") as w:
                        w.setnchannels(channels)
                        w.setsampwidth(SAMPLE_WIDTH)
                        w.setframerate(rate)
                        w.writeframes(pcm)
                
                    segments = await loop.run_in_executor(self.executor, self.pipeline._asr.transcribe, wav_path)
                    if not segments:
                        await self.send({"type": "no_speech", "seq": seq})
                        continue
                    
                    source_text = " ".join(s.text for s in segments)
                    if len(source_text.strip()) < 2:
                        await self.send({"type": "no_speech", "seq": seq})
                        continue
                    
                    await self.trans_queue.put((seq, source_text))
            except Exception as e:
                log.error("Ошибка ASR: %s", e)
            finally:
                self.asr_queue.task_done()

    async def translation_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            seq, source_text = await self.trans_queue.get()
            try:
                if self._tr_disabled:
                    continue
                translated = await loop.run_in_executor(self.executor, self.pipeline._translator.translate, source_text)
            
                pending = self.pipeline.take_pending_message()
                if pending:
                    await self.send(pending)
                
                await self.tts_queue.put((seq, translated))
            except TranslationError as exc:
                log.warning("Ошибка перевода: %s", exc)
                await self.send_error(f"Ошибка перевода: {exc}", seq)
            except Exception as e:
                log.error("Ошибка перевода: %s", e)
            finally:
                self.trans_queue.task_done()

    async def tts_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            seq, translated_text = await self.tts_queue.get()
            try:
                if self.subtitle_only or len(translated_text.strip()) < 3:
                    await self.send({"type": "subtitle", "seq": seq, "text": translated_text})
                    continue
            
                with tempfile.TemporaryDirectory(prefix="rt_tts_") as tmp:
                    wav_out_path = Path(tmp) / "chunk_ru.wav"
                    await loop.run_in_executor(self.executor, self.pipeline._tts.synthesize, translated_text, wav_out_path)
                
                    if wav_out_path.exists():
                        wav_bytes = wav_out_path.read_bytes()
                        payload = base64.b64encode(wav_bytes).decode("ascii")
                        await self.send({"type": "audio", "seq": seq, "format": "wav", "data": payload, "text": translated_text})
            except Exception as e:
                log.error("Ошибка TTS: %s", e)
            finally:
                self.tts_queue.task_done()

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

    # Делаем проверку Ollama НЕОБЯЗАТЕЛЬНОЙ
    try:
        await loop.run_in_executor(None, init_ollama)
    except OllamaSetupError as exc:
        log.warning("Ollama не запущена (это нормально, если вы используете облачный режим).")
        log.warning("Детали: %s", exc)

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline")
    warmup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="warmup") # <-- ДОБАВИТЬ
    log.info("Загрузка моделей...")
    app.state.asr, app.state.tts = await loop.run_in_executor(executor, build_shared_models)
    app.state.executor = executor
    app.state.warmup_executor = warmup_executor # <-- ДОБАВИТЬ
    log.info("Сервер готов")
    yield
    executor.shutdown(wait=False, cancel_futures=True)

def voices_info(tts_adapter) -> dict:
    if TTS_ENGINE == "xtts" and hasattr(tts_adapter, "list_voices"):
        return {"engine": "xtts", "voices": tts_adapter.list_voices(), "voice": tts_adapter.current_voice}
    elif TTS_ENGINE == "f5" and hasattr(tts_adapter, "list_voices"):
        return {"engine": "f5", "voices": tts_adapter.list_voices(), "voice": tts_adapter.current_voice}
    return {"engine": TTS_ENGINE, "voices": [], "voice": None}
    
app = FastAPI(title="Realtime audio translator", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

@app.get("/voices")
async def get_voices() -> dict:
    return voices_info(app.state.tts)

@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket) -> None:
    await ws.accept()
    origin = ws.headers.get("origin", "")
    # Разрешаем наши расширения во всех браузерах (Chrome, Opera, Edge, Yandex, Firefox, Brave)
    allowed_prefixes = (
        "chrome-extension://", 
        "opera-extension://", 
        "extension://", 
        "moz-extension://", 
        "browser-extension://"
    )
    # Также разрешаем пустой origin для тестов через curl/python-скрипты
    if origin != "" and not origin.startswith(allowed_prefixes):
        log.warning(f"Отклонено подключение с недопустимым Origin: {origin}")
        await ws.close(code=1008) # 1008 = Policy Violation
        return    
    # ИЗОЛИРОВАННЫЙ ПЕРЕВОДЧИК ДЛЯ КАЖДОЙ СЕССИИ
    local_translator = OpenAITranslationAdapter(
        source="en", target="ru", model=OLLAMA_MODEL, 
        base_url="http://127.0.0.1:11434/v1", api_key="ollama"
    )
    session_pipeline = TranslationPipeline(
        asr=app.state.asr, translator=local_translator, tts=app.state.tts
    )
    
    session = Session(ws, session_pipeline, app.state.executor, app.state.warmup_executor)
    worker_asr = asyncio.create_task(session.asr_worker())
    worker_trans = asyncio.create_task(session.translation_worker())
    worker_tts = asyncio.create_task(session.tts_worker())
    log.info("Клиент подключён: %s", ws.client)

    await session.send({
        "type": "ready", "format": session.format,
        "sample_rate": session.buffer.sample_rate,
        "channels": session.buffer.channels, "chunk_seconds": session.chunk_seconds,
        **voices_info(session_pipeline._tts)
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
        worker_asr.cancel()
        worker_trans.cancel()
        worker_tts.cancel()
        if session._warmup_task:
            session._warmup_task.cancel() # <-- ДОБАВИТЬ
        log.info("Клиент отключён: %s", ws.client)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), workers=1)