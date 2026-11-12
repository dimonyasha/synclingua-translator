import logging
import re
from pathlib import Path
from faster_whisper import WhisperModel
from .base import ASRAdapter, Segment

log = logging.getLogger(__name__)

# Фразы-галлюцинации Whisper на тишине/музыке
_HALLUCINATIONS = (
    "thanks for watching", "thank you for watching", "please subscribe",
    "subscribe to", "subtitles by", "amara org",
)

# Типичные междометия и мусор, которые Whisper путает с речью
_FILLERS = {
    "ara", "aha", "oh", "ah", "uh", "um", "yeah", "you", "the", "a", "an", 
    "and", "but", "so", "to", "it", "is", "of", "in", "on", "at", "by", 
    "my", "me", "we", "us", "he", "she", "ohh", "ahh"
}

def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text.lower()).strip()

def _is_hallucination(text: str) -> bool:
    t = " ".join(_norm(text).split())
    if any(p in t for p in _HALLUCINATIONS):
        return True
    words = t.split()
    
    # Если вся фраза состоит из одного слова, и это слово - междометие из списка
    if len(words) == 1 and words[0] in _FILLERS:
        return True
        
    # Зацикливания: "Thank you. Thank you. Thank you."
    return len(words) >= 3 and len(set(words)) <= max(1, len(words) // 3)

def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False

class FasterWhisperASR(ASRAdapter):
    LANG_CONFIDENCE = 0.6

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
            vad_parameters=dict(threshold=0.5, min_speech_duration_ms=250), # Чуть более чувствительный VAD
            condition_on_previous_text=False,
        )

    def transcribe(self, audio_path: Path) -> list[Segment]:
        segments_iter, info = self._run(audio_path, language=None)

        if info.duration_after_vad == 0:
            return []

        if info.language != self._language:
            if info.language_probability >= self.LANG_CONFIDENCE:
                log.info("Язык '%s' (%.2f) — пропуск", info.language, info.language_probability)
                return []
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
                log.info("Отброшена галлюцинация/междометие: %r", text)
                continue
                
            # Отбрасываем слишком короткие фразы (менее 4 символов, например "Ara.")
            clean = re.sub(r'[^\w\s]', '', text).strip()
            if len(clean) < 4: 
                log.info("Отброшена слишком короткая фраза: %r", text)
                continue
                
            result.append(Segment(s.start, s.end, text))
            
        return result