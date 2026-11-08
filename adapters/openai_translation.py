import logging
import re
import requests
import urllib3
import uuid
import os
from .base import TranslationAdapter
import certifi
import sys
from pathlib import Path

def _get_sber_ca_bundle() -> str:
    """Создает объединенный файл сертификатов (стандартные + Минцифры)."""
    # Ищем папку с сертификатами (учитывая сборку в .exe)
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent

    sber_cert_path = base / "certs" / "russian_trusted_root_ca.pem"
    
    # Если файла нет (например, забыли добавить при сборке), используем стандартные
    if not sber_cert_path.exists():
        log.warning("Сертификат Минцифры не найден! Используем стандартный certifi.")
        return certifi.where()

    # Безопасная папка в ProgramData (чтобы избежать проблем с кириллицей в путях)
    safe_dir = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "SyncLingua"
    safe_dir.mkdir(parents=True, exist_ok=True)
    out_path = safe_dir / "ca-sber-bundle.pem"

    # Склеиваем стандартные сертификаты и сертификат Сбера
    try:
        certifi_data = Path(certifi.where()).read_bytes()
        sber_data = sber_cert_path.read_bytes()
        out_path.write_bytes(certifi_data + b"\n" + sber_data)
        return str(out_path)
    except Exception as e:
        log.error("Ошибка создания SSL-бандла: %s", e)
        return certifi.where()
        
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
        
        # Если это GigaChat и включена проверка SSL, используем наш кастомный бандл
        if self._is_gigachat and verify_ssl:
            self._session.verify = _get_sber_ca_bundle()
        else:
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
            "английские или китайские символы. "
            "При переводе обращений используй форму 'ты', если в оригинале используется 'you' в единственном числе, и форму 'вы', если речь идёт о группе людей или официальном обращении. Не используй излишне формальное 'Вы' без необходимости."
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