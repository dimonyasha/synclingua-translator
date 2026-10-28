import logging
import os
import tempfile
import threading
import re
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from f5_tts.api import F5TTS

from .base import TTSAdapter

log = logging.getLogger(__name__)

# Пути к локальным файлам (в корне проекта)
F5_CKPT_PATH = "f5_russian_ckpt/F5TTS_v1_Base/model_240000_inference.safetensors"
F5_VOCAB_PATH = "f5_russian_ckpt/F5TTS_v1_Base/vocab.txt"
F5_OUTPUT_RATE = 24000


class F5TTSAdapter(TTSAdapter):
    def __init__(self, voices_dir: str | Path, default_voice: str = "reference_f5.wav",
                 device: str = "auto", language: str = "ru") -> None:
        self._voices_dir = Path(voices_dir).resolve()
        self._language = language

        # Блокировка для синтеза (F5-TTS не рассчитан на параллельные вызовы)
        self._lock = threading.Lock()

        # Определяем устройство
        has_cuda = torch.cuda.is_available()
        if device == "auto":
            device = "cuda" if has_cuda else "cpu"
        elif device == "cuda" and not has_cuda:
            log.warning("F5-TTS: CUDA недоступна, используется CPU (будет очень медленно)")
            device = "cpu"

        self._device = device
        self._speaker_wav = self._pick_initial_voice(default_voice)
        self._ref_text = "Красота начинается не с идеального образа, а с ощущения себя."  # Текст референса

        log.info("Загрузка F5-TTS (device=%s)...", device)
        self._tts = F5TTS(
            model="F5TTS_v1_Base",
            ckpt_file=F5_CKPT_PATH,
            vocab_file=F5_VOCAB_PATH,
            device=device
        )
        log.info("F5-TTS готов, голос: %s", self._speaker_wav.name)

    def _wav_files(self) -> list[Path]:
        if not self._voices_dir.is_dir():
            return []
        return sorted(
            (p for p in self._voices_dir.iterdir()
             if p.is_file() and p.suffix.lower() == ".wav"),
            key=lambda p: p.name.lower())

    def _resolve(self, voice_name: object) -> Path | None:
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
        if path.stem.lower() == "reference":
            return "По умолчанию"
        name = path.stem.removeprefix("reference_").replace("_", " ").replace("-", " ")
        return name.strip().title() or path.stem

    def list_voices(self) -> list[dict]:
        return [{"file": p.name, "label": self._label(p)} for p in self._wav_files()]

    @property
    def current_voice(self) -> str:
        return self._speaker_wav.name

    def change_voice(self, voice_name: str) -> bool:
        path = self._resolve(voice_name)
        if path is None:
            log.error("F5-TTS: образец %r не найден", voice_name)
            return False
        if path != self._speaker_wav:
            self._speaker_wav = path
            log.info("F5-TTS: выбран голос %s", path.name)
        return True

    @staticmethod
    def _split_text(text: str, max_chars: int = 180) -> list[str]:
        """Разбивает текст на куски по предложениям, чтобы не обрывать слова."""
        # Сначала по точкам/вопросам/восклицаниям
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        chunks = []
        current = ""
        for s in sentences:
            if len(current) + len(s) + 1 <= max_chars:
                current = f"{current} {s}".strip()
            else:
                if current:
                    chunks.append(current)
                # Если предложение всё ещё длинное — режем по запятым
                if len(s) > max_chars:
                    parts = re.split(r'(?<=,)\s+', s)
                    current = ""
                    for p in parts:
                        if len(current) + len(p) + 1 <= max_chars:
                            current = f"{current} {p}".strip()
                        else:
                            if current:
                                chunks.append(current)
                            current = p
                    if current:
                        chunks.append(current)
                else:
                    current = s
        if current:
            chunks.append(current)
        return chunks if chunks else [text]

    def _prepare_text(self, text: str) -> str:
        """Заменяет цифры на слова и убирает лишние символы."""
        from num2words import num2words
        def replace_number(match):
            num_str = match.group(0)
            try:
                if '.' in num_str or ',' in num_str:
                    num_str = num_str.replace(',', '.')
                    return num2words(float(num_str), lang='ru')
                else:
                    return num2words(int(num_str), lang='ru')
            except:
                return num_str
        text = re.sub(r'\d+[.,]?\d*', replace_number, text)
        text = re.sub(r'[^\w\s.,!?;:—–-]', '', text)
        return text

    def synthesize(self, text: str, output_path: Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if not text.strip():
                raise ValueError("Пустой текст для синтеза")

            # --- ФИЛЬТР СЛИШКОМ КОРОТКИХ ФРАЗ (чтобы не было "Вы-Вы-Вы") ---
            clean = text.strip()
            if len(clean) < 10:
                log.warning("F5-TTS: слишком короткий текст '%s', пропускаем", clean)
                import numpy as np
                silence = np.zeros(F5_OUTPUT_RATE // 2, dtype=np.float32)
                sf.write(str(output_path), silence, F5_OUTPUT_RATE)
                return output_path
            # Добавляем точку, если её нет (помогает модели понять конец фразы)
            if clean[-1] not in ".!?":
                clean += "."
            # -------------------------------------------------------------
            text = self._prepare_text(text)
            # Разбиваем длинный текст на фразы (по точкам, запятым, длине)
            chunks = self._split_text(clean, max_chars=120)
            all_wavs = []

            with self._lock:
                for chunk in chunks:
                    # Для коротких фраз даём больше шагов (лучше качество), для длинных — меньше (быстрее)
                    steps = 16 if len(chunk) < 30 else 8
                    wav, sr, _ = self._tts.infer(
                        ref_file=str(self._speaker_wav),
                        ref_text=self._ref_text,
                        gen_text=chunk,
                        nfe_step=steps,
                        show_info=lambda msg: None
                    )
                    all_wavs.append((wav, sr))

            # Склеиваем аудио
            if len(all_wavs) == 1:
                final_wav, final_sr = all_wavs[0]
            else:
                import numpy as np
                final_sr = all_wavs[0][1]
                final_wav = np.concatenate([w for w, _ in all_wavs])

            sf.write(str(output_path), final_wav, final_sr)
            return output_path

        except Exception as e:
            log.exception("F5-TTS: ошибка синтеза")
            import numpy as np
            silence = np.zeros(F5_OUTPUT_RATE, dtype=np.float32)
            sf.write(str(output_path), silence, F5_OUTPUT_RATE)
            return output_path