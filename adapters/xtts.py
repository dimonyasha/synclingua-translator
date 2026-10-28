"""TTS-адаптер на базе Coqui XTTS v2: клонирование голоса, выбор образца из voices/."""
import logging
import os
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np
import torch
from TTS.api import TTS

from .base import TTSAdapter

log = logging.getLogger(__name__)

MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
OUTPUT_RATE = 24000  # XTTS v2: 24 кГц, моно


class XTTSAdapter(TTSAdapter):
    def __init__(self, voices_dir: str | Path, default_voice: str = "reference.wav",
                 device: str = "auto", language: str = "ru",
                 fail_silently: bool = False) -> None:
        self._voices_dir = Path(voices_dir).resolve()
        self._language = language
        self._fail_silently = fail_silently

        # Два замка с разными задачами:
        #  _synth_lock - один синтез за раз (долгий);
        #  _state_lock - защита голоса и кэша, держится микросекунды, поэтому
        #                change_voice() можно вызывать из event loop без блокировок.
        self._synth_lock = threading.Lock()
        self._state_lock = threading.Lock()

        # (путь к образцу, (gpt_cond_latent, speaker_embedding)) или None
        self._cached_latents: tuple[Path, tuple] | None = None
        self._speaker_wav: Path = self._pick_initial_voice(default_voice)

        has_cuda = torch.cuda.is_available()
        if device == "auto":
            device = "cuda" if has_cuda else "cpu"
        elif device == "cuda" and not has_cuda:
            log.warning("XTTS: CUDA недоступна, используется CPU (будет очень медленно)")
            device = "cpu"

        log.info("Загрузка XTTS v2 (device=%s)...", device)
        self._tts = TTS(MODEL_NAME).to(device)
        # Низкоуровневая модель Xtts: через неё латенты считаются отдельно от синтеза
        self._model = self._tts.synthesizer.tts_model

        # Прогрев: анализируем стартовый образец сейчас, а не на первой фразе
        self._get_latents(self._speaker_wav)
        log.info("XTTS готов, голос: %s", self._speaker_wav.name)

    # ------------------------------------------------------------ голоса
    def _wav_files(self) -> list[Path]:
        if not self._voices_dir.is_dir():
            return []
        return sorted(
            (p for p in self._voices_dir.iterdir()
             if p.is_file() and p.suffix.lower() == ".wav"),
            key=lambda p: p.name.lower())

    def _resolve(self, voice_name: object) -> Path | None:
        """Имя файла -> путь внутри voices/. None, если имя небезопасно или файла нет.

        Имя приходит от клиента, поэтому пути вида '../x.wav' отсекаем."""
        if not isinstance(voice_name, str) or Path(voice_name).name != voice_name:
            return None
        if not voice_name.lower().endswith(".wav"):
            return None
        path = self._voices_dir / voice_name
        return path if path.is_file() else None

    def _pick_initial_voice(self, default_voice: str) -> Path:
        path = self._resolve(default_voice)
        if path:
            return path
        available = self._wav_files()
        if not available:
            raise FileNotFoundError(f"В {self._voices_dir} нет ни одного .wav-образца")
        log.warning("Голос %s не найден, использую %s", default_voice, available[0].name)
        return available[0]

    @staticmethod
    def _label(path: Path) -> str:
        """reference_trump.wav -> 'Trump', reference.wav -> 'По умолчанию'."""
        if path.stem.lower() == "reference":
            return "По умолчанию"
        name = path.stem.removeprefix("reference_").replace("_", " ").replace("-", " ")
        return name.strip().title() or path.stem

    def list_voices(self) -> list[dict]:
        """Папка сканируется при каждом вызове: новый файл появляется без перезапуска."""
        return [{"file": p.name, "label": self._label(p)} for p in self._wav_files()]

    @property
    def current_voice(self) -> str:
        with self._state_lock:
            return self._speaker_wav.name

    def change_voice(self, voice_name: str) -> bool:
        """Переключить образец голоса. True - голос выбран, False - файла нет (голос не менялся).

        Метод быстрый: латенты нового образца посчитаются при ближайшем синтезе
        (в потоке пайплайна), там и будет пауза на несколько секунд."""
        path = self._resolve(voice_name)
        if path is None:
            log.error("XTTS: образец %r не найден в %s, голос не изменён",
                      voice_name, self._voices_dir)
            return False
        with self._state_lock:
            if path == self._speaker_wav:
                return True  # тот же голос: кэш не сбрасываем
            self._speaker_wav = path
            self._cached_latents = None  # заставляем XTTS заново проанализировать файл
        log.info("XTTS: выбран голос %s", path.name)
        return True

    def _get_latents(self, wav: Path) -> tuple:
        """Латенты образца: из кэша или считаем (секунды на GPU, дольше на CPU)."""
        with self._state_lock:
            cached = self._cached_latents
        if cached is not None and cached[0] == wav:
            return cached[1]

        log.info("XTTS: анализ образца %s...", wav.name)
        latents = self._model.get_conditioning_latents(audio_path=[str(wav)])

        with self._state_lock:
            # Пока считали, голос могли сменить: устаревшие латенты в кэш не кладём
            if self._speaker_wav == wav:
                self._cached_latents = (wav, latents)
        return latents

    # ------------------------------------------------------------ синтез
    def synthesize(self, text: str, output_path: Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_wav: Path | None = None
        try:
            if not text.strip():
                raise ValueError("Пустой текст для синтеза")

            with self._synth_lock:
                with self._state_lock:
                    sample = self._speaker_wav  # снимок: смена голоса применится со следующего куска
                gpt_cond_latent, speaker_embedding = self._get_latents(sample)
                out = self._model.inference(
                    text=text,
                    language=self._language,
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    enable_text_splitting=True,  # длинные фразы XTTS режет по предложениям
                )

            # Временный WAV -> атомарное переименование в output_path (как и раньше)
            fd, name = tempfile.mkstemp(suffix=".wav", dir=output_path.parent)
            os.close(fd)
            tmp_wav = Path(name)
            self._write_wav(tmp_wav, out["wav"])
            os.replace(tmp_wav, output_path)
            tmp_wav = None
            return output_path

        except Exception:
            log.exception("XTTS: ошибка синтеза")
            if not self._fail_silently:
                raise  # сервер отправит клиенту {"type":"error"}, соединение останется
            self._write_silence(output_path)
            return output_path
        finally:
            if tmp_wav is not None:
                tmp_wav.unlink(missing_ok=True)

    @staticmethod
    def _write_wav(path: Path, samples) -> None:
        """float32 [-1..1] (numpy или torch) -> 16-bit PCM WAV."""
        arr = torch.as_tensor(samples).detach().cpu().numpy().squeeze()
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUTPUT_RATE)
            w.writeframes(pcm.tobytes())

    @staticmethod
    def _write_silence(path: Path, seconds: float = 0.3) -> None:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUTPUT_RATE)
            w.writeframes(b"\x00\x00" * int(OUTPUT_RATE * seconds))