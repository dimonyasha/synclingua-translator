import logging
import re
from pathlib import Path
from faster_whisper import WhisperModel
from .base import ASRAdapter, Segment

log = logging.getLogger(__name__)

# Фразы-галлюцинации Whisper на тишине/музыке. Только длинные, чтобы не резать
# настоящую речь вроде "Thank you." (её отсеет фильтр по метрикам).
_HALLUCINATIONS = (
    "thanks for watching", "thank you for watching", "please subscribe",
    "subscribe to", "subtitles by", "amara org",
)

def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text.lower()).strip()

def _is_hallucination(text: str) -> bool:
    t = " ".join(_norm(text).split())
    if any(p in t for p in _HALLUCINATIONS):
        return True
    words = t.split()
    # "Thank you. Thank you. Thank you." и подобные зацикливания
    return len(words) >= 3 and len(set(words)) <= max(1, len(words) // 3)

def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False

class FasterWhisperASR(ASRAdapter):
    LANG_CONFIDENCE = 0.6   # выше этого уверенно считаем, что язык не наш

    def __init__(self, model_size: str = "small", device: str = "auto", language: str = "en") -> None:
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"

        log.info("Загрузка Whisper '%s' (device=%s)...", model_size, device)
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._language = language

    def _run(self, audio_path: Path, language: str | None):
        return self._model.transcribe(
            str(audio_path),
            language=language,
            beam_size=5,
            vad_filter=True,
            # СМЯГЧАЕМ ПАРАМЕТРЫ VAD:
            vad_parameters=dict(
                threshold=0.4,                  # Было 0.6. Снижаем, чтобы ловить тихую речь
                min_speech_duration_ms=100,     # Было 250. Снижаем, чтобы ловить короткие слова
                min_silence_duration_ms=500     # Пауза 0.5 сек для завершения фразы
            ),
            condition_on_previous_text=False,
        )

    def transcribe(self, audio_path: Path) -> list[Segment]:
        # Язык определяется сразу при вызове, до декодирования текста
        segments_iter, info = self._run(audio_path, language=None)

        # Если VAD вырезал ВСЁ аудио (чистая тишина/шум) — сразу выходим!
        if info.duration_after_vad == 0:
            return []

        if info.language != self._language:
            if info.language_probability >= self.LANG_CONFIDENCE:
                log.info("Язык '%s' (%.2f) — пропуск", info.language, info.language_probability)
                return []
            # Не уверены (шум, короткий кусок) — считаем, что это английский
            segments_iter, info = self._run(audio_path, language=self._language)

        result = []
        for s in segments_iter:
            text = s.text.strip()
            if not text:
                continue
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            if s.avg_logprob < -1.2 or s.compression_ratio > 2.4:
                continue
            if _is_hallucination(text):
                log.info("Отброшена галлюцинация: %r", text)
                continue
            result.append(Segment(s.start, s.end, text))
        return result
