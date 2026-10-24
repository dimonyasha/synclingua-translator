import logging
from pathlib import Path
from faster_whisper import WhisperModel
from .base import ASRAdapter, Segment

log = logging.getLogger(__name__)

def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False

class FasterWhisperASR(ASRAdapter):
    def __init__(self, model_size: str = "base", device: str = "auto", language: str = "en") -> None:
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        
        log.info("Загрузка Whisper '%s' (device=%s)...", model_size, device)
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._language = language

    def transcribe(self, audio_path: Path) -> list[Segment]:
        segments_iter, info = self._model.transcribe(
            str(audio_path), language=self._language, vad_filter=True, beam_size=5
        )
        return [Segment(s.start, s.end, s.text.strip()) for s in segments_iter if s.text.strip()]