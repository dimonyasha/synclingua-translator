import logging
import urllib.request
import wave
from pathlib import Path
from piper.voice import PiperVoice
from .base import TTSAdapter

log = logging.getLogger(__name__)

MODELS = {
    "male_1": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx",
    "female_1": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx",
    "male_2": "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx"
}

class EdgeTTSAdapter(TTSAdapter): 
    def __init__(self, voice_type: str = "male_1") -> None:
        self._voice_name = voice_type
        self._model_dir = Path("piper_models")
        self._model_dir.mkdir(exist_ok=True)
        
        url = MODELS.get(voice_type, MODELS["male_1"])
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
        
    def change_voice(self, voice_type: str):
        log.info("Смена голоса на %s...", voice_type)
        url = MODELS.get(voice_type, MODELS["male_1"])
        onnx_path = self._model_dir / f"{voice_type}.onnx"
        json_path = self._model_dir / f"{voice_type}.onnx.json"
        
        if not onnx_path.exists():
            log.info("Скачивание новой модели голоса...")
            urllib.request.urlretrieve(url, onnx_path)
            urllib.request.urlretrieve(url + ".json", json_path)
            
        self._voice = PiperVoice.load(str(onnx_path))
        
    def synthesize(self, text: str, output_path: Path) -> Path:
        clean_text = text.strip(" .,!?-—\n\t")
        if not clean_text:
            return output_path

        wav_path = output_path.with_suffix('.wav')
        
        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._voice.config.sample_rate)
            
            # Универсальный вызов: поддерживает и новые, и старые версии piper-tts
            if hasattr(self._voice, 'synthesize_wav'):
                self._voice.synthesize_wav(clean_text, wav_file)
            else:
                self._voice.synthesize(clean_text, wav_file)
                
        # Проверяем, что файл не пустой
        if wav_path.stat().st_size < 100:
            log.error("ВНИМАНИЕ: Piper TTS сгенерировал пустой файл! Возможно, ошибка в модели.")
            
        # Переименовываем в .mp3 (браузер всё равно проиграет WAV-байты, 
        # так как мы передаем их как base64 и используем decodeAudioData)
        wav_path.rename(output_path)
        return output_path