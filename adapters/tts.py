import logging
import urllib.request
import wave
import sys
import os
import io
from pathlib import Path
from paths import get_base_path
from piper.voice import PiperVoice
from .base import TTSAdapter

log = logging.getLogger(__name__)

MODELS = {
    "male_1": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx",
    "female_1": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx",
    "male_2": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx",
    "male_3": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx"
}

class EdgeTTSAdapter(TTSAdapter): 
    def __init__(self, voice_type: str = "male_1") -> None:
        self._voice_name = voice_type
        
        # Если мы в .exe, используем безопасную папку в ProgramData
        if getattr(sys, 'frozen', False):
            _safe_dir = os.path.join(os.environ.get('PROGRAMDATA', 'C:\\ProgramData'), 'SyncLingua', 'piper_models')
            os.makedirs(_safe_dir, exist_ok=True)
            self._model_dir = Path(_safe_dir)
        else:
            self._model_dir = get_base_path() / "piper_models"
            self._model_dir.mkdir(exist_ok=True)
        
        url = MODELS.get(voice_type, MODELS["male_1"])
        # Теперь self._model_dir существует, и код ниже сработает
        onnx_path = self._model_dir / f"{voice_type}.onnx"
        json_path = self._model_dir / f"{voice_type}.onnx.json"
        
        # Скачиваем модель, если её нет
        if not onnx_path.exists() or onnx_path.stat().st_size < 1000:
            log.info("Скачивание локальной модели голоса %s (около 60 МБ)...", voice_type)
            urllib.request.urlretrieve(url, onnx_path)
            urllib.request.urlretrieve(url + ".json", json_path)
            log.info("Модель успешно скачана.")
            
        log.info("Загрузка Piper TTS в память...")
        self._voice = PiperVoice.load(str(onnx_path))
        
    def change_voice(self, voice_type: str) -> bool:
        log.info("Смена голоса на %s...", voice_type)
        try:
            url = MODELS.get(voice_type, MODELS["male_1"])
            onnx_path = self._model_dir / f"{voice_type}.onnx"
            json_path = self._model_dir / f"{voice_type}.onnx.json"
            
            if not onnx_path.exists() or onnx_path.stat().st_size < 1000:
                log.info("Скачивание новой модели голоса %s...", voice_type)
                import urllib.request
                urllib.request.urlretrieve(url, onnx_path)
                urllib.request.urlretrieve(url + ".json", json_path)
                
            self._voice = PiperVoice.load(str(onnx_path))
            self._voice_name = voice_type   # <--- ДОБАВИТЬ ЭТУ СТРОКУ
            log.info("Голос %s успешно загружен", voice_type)
            return True
        except Exception as e:
            log.error("Ошибка смены голоса %s: %s", voice_type, e)
            return False
        
    @property
    def current_voice(self) -> str:
        return self._voice_name
        
    def synthesize(self, text: str, output_path: Path) -> Path:
        clean_text = text.strip(" .,!?-—\n\t")
        if not clean_text:
            return output_path

        # Создаем буфер в памяти вместо файла на диске
        wav_buffer = io.BytesIO()
        
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._voice.config.sample_rate)
            
            # Универсальный вызов: поддерживает и новые, и старые версии piper-tts
            if hasattr(self._voice, 'synthesize_wav'):
                self._voice.synthesize_wav(clean_text, wav_file)
            else:
                self._voice.synthesize(clean_text, wav_file)
                
        # Получаем байты из памяти
        wav_bytes = wav_buffer.getvalue()
        
        if len(wav_bytes) < 100:
            log.error("ВНИМАНИЕ: Piper TTS сгенерировал пустой файл!")
            
        # Сохраняем сразу в итоговый файл (браузер всё равно проиграет WAV-байты)
        output_path.write_bytes(wav_bytes)
        return output_path