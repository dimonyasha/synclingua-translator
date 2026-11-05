import logging
import re
import requests
import urllib3
import uuid
from .base import TranslationAdapter

# Отключаем предупреждения об отключенном SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

class TranslationError(Exception):
    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal

class OpenAITranslationAdapter(TranslationAdapter):
    def __init__(self, source: str = "en", target: str = "ru",
                 model: str = "translategemma:4b",
                 base_url: str = "http://127.0.0.1:11434/v1",
                 api_key: str = "ollama", 
                 verify_ssl: bool = True) -> None:
        self._source, self._target, self._model = source, target, model
        self._verify_ssl = verify_ssl
        self._api_key = api_key

        base = base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        self._base_url = base
        self._url = f"{base}/chat/completions"
        
        # Надежная проверка на GigaChat
        self._is_gigachat = "giga.chat" in base.lower() or "sberbank" in base.lower()
        self._giga_token = None

        self._session = requests.Session()
        self._session.verify = verify_ssl

        log.info("Перевод: модель=%s, url=%s, ssl_verify=%s", model, base, verify_ssl)

    def close(self) -> None:
        if hasattr(self, "_session"):
            self._session.close()

    def _get_gigachat_token(self) -> str:
        """Получает Access Token для GigaChat по Authorization Key."""
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {self._api_key}"
        }
        data = {"scope": "GIGACHAT_API_PERS"}
        
        try:
            resp = self._session.post(url, headers=headers, data=data, timeout=(3, 8))
            if resp.status_code != 200:
                log.error("Ошибка получения токена GigaChat: %s", resp.text)
                raise TranslationError(f"Ошибка авторизации GigaChat (HTTP {resp.status_code})", fatal=True)
            
            token = resp.json().get("access_token")
            if not token:
                raise TranslationError("GigaChat не вернул access_token", fatal=True)
            return token
        except requests.RequestException as e:
            raise TranslationError(f"Нет связи с сервером авторизации GigaChat: {e}", fatal=True)

    def translate(self, text: str) -> str:
        system_prompt = (
            "Ты профессиональный синхронный переводчик. Твоя единственная задача — "
            "переводить текст с английского на русский язык. Выводи ТОЛЬКО переведенный "
            "текст на русском. Запрещено выводить твои мысли, объяснения, комментарии, "
            "английские или китайские символы."
        )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
            "max_tokens": min(1024, max(128, len(text))),
        }

        # Если это GigaChat, получаем токен
        if self._is_gigachat:
            if not self._giga_token:
                self._giga_token = self._get_gigachat_token()
            self._session.headers.update({
                "Authorization": f"Bearer {self._giga_token}",
                "Content-Type": "application/json",
            })
        else:
            # Для OpenRouter / Ollama
            self._session.headers.update({
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            })

        try:
            resp = self._session.post(self._url, json=payload, timeout=(3, 8))
        except requests.Timeout:
            raise TranslationError("Таймаут перевода")
        except requests.ConnectionError:
            if "127.0.0.1" in self._url or "localhost" in self._url:
                raise TranslationError("Ollama не запущена или недоступна. Проверьте трей.", fatal=True)
            raise TranslationError("Нет связи с сервером перевода")

        # Если токен GigaChat протух, пробуем обновить
        if self._is_gigachat and resp.status_code == 401:
            log.warning("Токен GigaChat протух, запрашиваем новый...")
            self._giga_token = self._get_gigachat_token()
            self._session.headers.update({"Authorization": f"Bearer {self._giga_token}"})
            try:
                resp = self._session.post(self._url, json=payload, timeout=(3, 8))
            except requests.RequestException as e:
                raise TranslationError(f"Повторный запрос не удался: {e}")

        if resp.status_code in (401, 403):
            raise TranslationError("Неверный API-ключ или нет доступа", fatal=True)
        if resp.status_code == 429:
            raise TranslationError("Превышен лимит запросов провайдера")
        if not resp.ok:
            raise TranslationError(f"Ошибка провайдера (HTTP {resp.status_code})")

        try:
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError):
            raise TranslationError("Неожиданный ответ провайдера")

        out = re.sub(r"[\u4e00-\u9fff]+", "", content)
        out = out.replace("Перевод:", "").replace("Translation:", "").strip()

        # --- ИСПРАВЛЕНО: Фильтр ТОЛЬКО для GigaChat ---
        if self._is_gigachat:
            lower_out = out.lower()
            if any(phrase in lower_out for phrase in [
                "языковые модели", "не обладают собственным мнением", 
                "чувствительные темы", "временно ограничены", "открытом доступе"
            ]):
                log.warning("GigaChat отказался переводить фразу (сработал фильтр). Пропускаем.")
                raise TranslationError("GigaChat заблокировал фразу", fatal=False)

        if not out:
            raise TranslationError("Пустой ответ модели")
            
        return out