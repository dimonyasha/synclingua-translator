import logging
from pathlib import Path
from adapters import ASRAdapter, TranslationAdapter, TTSAdapter

log = logging.getLogger(__name__)

class TranslationPipeline:
    def __init__(self, asr: ASRAdapter, translator: TranslationAdapter, tts: TTSAdapter) -> None:
        self._asr = asr
        self._translator = translator
        self._tts = tts

    def run(self, input_path: Path, output_path: Path | None = None, save_text: bool = False) -> Path:
        output_path = output_path or input_path.with_name(f"{input_path.stem}_ru.mp3")
        segments = self._asr.transcribe(input_path)
        if not segments:
            raise RuntimeError("Речь в файле не обнаружена.")
        source_text = " ".join(s.text for s in segments)
        
        translated = self._translator.translate(source_text)
        
        result = self._tts.synthesize(translated, output_path)
        if save_text:
            input_path.with_name(f"{input_path.stem}_en.txt").write_text(source_text, encoding="utf-8")
            input_path.with_name(f"{input_path.stem}_ru.txt").write_text(translated, encoding="utf-8")
            
        # Возвращаем и путь к файлу, и переведенный текст
        return result, translated 