import logging
import requests
import re
from .base import TranslationAdapter

log = logging.getLogger(__name__)

class OllamaTranslationAdapter(TranslationAdapter):
    def __init__(self, source: str = "en", target: str = "ru", model: str = "qwen2.5:7b") -> None:
        self._source = source
        self._target = target
        self._model = model
        self._url = "http://localhost:11434/api/chat"
        log.info("Инициализация перевода через Ollama (модель: %s)...", self._model)

    def translate(self, text: str) -> str:
        # Жесткий системный промпт: запрещаем болтать и использовать китайский
        system_prompt = (
            "Ты профессиональный синхронный переводчик. Твоя единственная задача — переводить текст с английского на русский язык. "
            "Выводи ТОЛЬКО переведенный текст на русском. Запрещено выводить твои мысли, объяснения, комментарии, английские или китайские символы. "
            "Если в тексте есть имена собственные, транслитерируй их на русский."
        )
        
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            "stream": False, # Ждем полный ответ
            "options": {
                "temperature": 0.3,     # Чуть выше, чтобы не зацикливалась
                "num_predict": 150,
                "repeat_penalty": 1.2   # ШТРАФ ЗА ПОВТОРЕНИЯ (убирает "3, 3, 3")
            }
        }
        
        try:
            response = requests.post(self._url, json=payload, timeout=15)
            response.raise_for_status()
            result = response.json()
            
            translated_text = result.get("message", {}).get("content", "").strip()
            
            # ЖЕЛЕЗНАЯ ОЧИСТКА: Если нейросеть что-то намудрила, мы это отрежем
            # Удаляем китайские символы и всё, что похоже на "Перевод:"
            translated_text = re.sub(r'[\u4e00-\u9fff]+', '', translated_text) # Удаляем китайский
            translated_text = translated_text.replace("Перевод:", "").replace("Translation:", "").strip()
            
            # Если после очистки ничего не осталось (нейросеть выдала только мысли), возвращаем оригинал
            if not translated_text:
                return text
                
            return translated_text
        except Exception as e:
            log.error("Ошибка Ollama: %s", e)
            return text