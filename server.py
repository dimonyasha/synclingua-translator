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
  {"type": "audio", "seq": N, "format": "mp3", "data": "<base64>"}
  {"type": "no_speech", "seq": N}         # в чанке не было речи
  {"type": "error", "seq": N|null, "message": "..."}
"""
import os
import sys
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
os.environ["ARGOS_PACKAGES_DIR"] = r"z:\Projects\LiveVoice\audio_translator\argos_models"
import asyncio
import base64
import binascii
import io
import json
import logging
import os
import tempfile
import wave
import requests
from huggingface_hub import set_client_factory

def create_client_without_ssl_verify():
    session = requests.Session()
    session.verify = False
    return session

set_client_factory(create_client_without_ssl_verify)
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket

from pipeline import TranslationPipeline

from adapters.ollama_translation import OllamaTranslationAdapter

log = logging.getLogger("server")

# ---------------------------------------------------------------- настройки
DEFAULT_SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
CHUNK_SECONDS = 3.0  # Было 2.0. 
MIN_SECONDS = float(os.getenv("MIN_SECONDS", "0.5"))
MAX_QUEUE = 3        # Было 5. Теперь сервер хранит только 1 кусок в очереди. Если не успевает — выкидывает старые.
ASR_MODEL = os.getenv("ASR_MODEL", "base")
ASR_DEVICE = os.getenv("ASR_DEVICE", "auto")
SAMPLE_WIDTH = 2  # PCM16 = 2 байта на сэмпл

# Маркер сообщения RuntimeError из pipeline.py, когда речь не найдена.
# (Надёжнее завести в pipeline.py отдельный NoSpeechError и ловить его.)
NO_SPEECH_MARKER = "Речь"


# ---------------------------------------------------------------- пайплайн
def build_pipeline() -> TranslationPipeline:
    from adapters import FasterWhisperASR, EdgeTTSAdapter

    return TranslationPipeline(
        asr=FasterWhisperASR(model_size=ASR_MODEL, device=ASR_DEVICE),
        # ИСПОЛЬЗУЕМ OLLAMA ВМЕСТО ARGOS
        translator=OllamaTranslationAdapter(source="en", target="ru", model="qwen2.5:7b"),
        tts=EdgeTTSAdapter(voice_type="male_1"), 
    )


def translate_pcm(pipeline: TranslationPipeline, pcm: bytes,
                  sample_rate: int, channels: int) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory(prefix="rt_translate_") as tmp:
        wav_path = Path(tmp) / "chunk.wav"
        mp3_path = Path(tmp) / "chunk_ru.mp3"

        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(sample_rate)
            w.writeframes(pcm)

        # Получаем и файл, и текст
        _, translated_text = pipeline.run(wav_path, mp3_path)
        return mp3_path.read_bytes(), translated_text


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
        """Забрать всё накопленное, выровняв по границе кадра.

        Хвост, не образующий целый кадр (если чанк разрезал сэмпл),
        остаётся в буфере до следующего чанка.
        """
        usable = len(self._data) - (len(self._data) % self.frame_bytes)
        chunk = bytes(self._data[:usable])
        del self._data[:usable]
        return chunk

    def clear(self) -> None:
        self._data.clear()


# ---------------------------------------------------------------- сессия
class Session:
    """Состояние одного WebSocket-соединения.

    Приём чанков и тяжёлая обработка разделены: приём идёт в основном цикле,
    а переводом занимается фоновый worker. Поэтому новые чанки принимаются,
    пока предыдущий кусок ещё переводится.
    """

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

    # ---- отправка
    async def send(self, payload: dict) -> None:
        """Отправка с блокировкой (пишут и приёмник, и worker). Не бросает."""
        async with self._send_lock:
            try:
                await self.ws.send_json(payload)
            except Exception as exc:  # клиент мог уже отключиться
                log.warning("Не удалось отправить клиенту: %s", exc)

    async def send_error(self, message: str, seq: int | None = None) -> None:
        log.error("Ошибка (seq=%s): %s", seq, message)
        await self.send({"type": "error", "seq": seq, "message": message})

    # ---- приём
    async def handle_message(self, msg: dict) -> None:
        """Обработка одного сообщения из ws.receive()."""
        if msg.get("bytes") is not None:            # бинарный фрейм = сырой чанк
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
        elif mtype == "set-voice":
            voice = data.get("voice", "male_1")
            # Меняем голос в пайплайне (нам нужно добавить метод смены голоса в TTS адаптер)
            if hasattr(self.pipeline._tts, 'change_voice'):
                self.pipeline._tts.change_voice(voice)
                await self.send({"type": "info", "message": f"voice changed to {voice}"})        
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
        self.buffer = AudioBuffer(rate, channels)  # новый формат — новый буфер
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

        # Авто-flush: накопилось достаточно аудио
        if self.buffer.seconds >= CHUNK_SECONDS:
            await self._flush()

    async def _flush(self) -> None:
        """Забрать буфер и поставить в очередь на обработку."""
        if self.buffer.seconds < MIN_SECONDS:
            await self.send({"type": "info", "message": "buffer too short, skipped"})
            return
        
        pcm = self.buffer.take()
        self.seq += 1
        job = (self.seq, pcm, self.buffer.sample_rate, self.buffer.channels)
        
        # Если очередь забита, выкидываем старые задачи, чтобы не отставать
        if self.queue.full():
            try:
                self.queue.get_nowait() 
                self.queue.task_done()
                log.warning("Очередь переполнена, старый кусок отброшен")
            except asyncio.QueueEmpty:
                pass
                
        self.queue.put_nowait(job)

    # ---- фоновая обработка
    async def worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            seq, pcm, rate, channels = await self.queue.get()
            try:
                # Блокирующий пайплайн — в отдельном потоке
                mp3, text = await loop.run_in_executor(
                    self.executor, translate_pcm, self.pipeline, pcm, rate, channels)
                payload = base64.b64encode(mp3).decode("ascii")
                
                # Отправляем и аудио, и текст
                await self.send({"type": "audio", "seq": seq,
                                 "format": "mp3", "data": payload, "text": text})
                log.info("seq=%d готов (%d КБ mp3)", seq, len(mp3) // 1024)
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


# ---------------------------------------------------------------- приложение
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Один поток на пайплайн: Whisper-модель общая и тяжёлая, запросы
    # от всех клиентов выполняются строго по очереди.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
    loop = asyncio.get_running_loop()
    log.info("Загрузка моделей...")
    app.state.pipeline = await loop.run_in_executor(executor, build_pipeline)
    app.state.executor = executor
    log.info("Сервер готов")
    yield
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Realtime audio translator", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket) -> None:
    await ws.accept()
    session = Session(ws, app.state.pipeline, app.state.executor)
    worker = asyncio.create_task(session.worker())
    log.info("Клиент подключён: %s", ws.client)

    await session.send({
        "type": "ready", "format": session.format,
        "sample_rate": session.buffer.sample_rate,
        "channels": session.buffer.channels, "chunk_seconds": CHUNK_SECONDS,
    })

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            try:
                await session.handle_message(msg)
            except Exception as exc:  # что бы ни случилось — соединение не рвём
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
    # Один процесс: модель живёт в памяти, несколько воркеров загрузили бы её N раз
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")), workers=1)