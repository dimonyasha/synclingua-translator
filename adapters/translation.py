import logging
import argostranslate.package
import argostranslate.translate
from .base import TranslationAdapter

log = logging.getLogger(__name__)

class GoogleTranslationAdapter(TranslationAdapter):
    def __init__(self, source: str = "en", target: str = "ru") -> None:
        self._source = source
        self._target = target
        
        # Установка модели (как и раньше)
        log.info("Проверка локальной модели перевода Argos (en -> ru)...")
        argostranslate.package.update_package_index()
        available_packages = argostranslate.package.get_available_packages()
        package_to_install = next(
            filter(lambda x: x.from_code == self._source and x.to_code == self._target, available_packages),
            None
        )
        if package_to_install:
            try:
                log.info("Установка языковой модели Argos...")
                argostranslate.package.install_from_path(package_to_install.download())
            except Exception:
                pass
        
        # Получаем объекты языков один раз при инициализации
        self._installed_languages = argostranslate.translate.get_installed_languages()
        log.info("Установленные языки: %s", [lang.code for lang in self._installed_languages])
        
        self._from_lang = next((lang for lang in self._installed_languages if lang.code == self._source), None)
        self._to_lang = next((lang for lang in self._installed_languages if lang.code == self._target), None)
        
        if not self._from_lang or not self._to_lang:
            raise RuntimeError(f"Не удалось найти языки {self._source} или {self._target} в Argos.")
        
        self._translation = self._from_lang.get_translation(self._to_lang)
        log.info("Модель Argos готова к работе.")

    def translate(self, text: str) -> str:
        log.info("Перевод (Argos): %d символов...", len(text))
        try:
            # Используем заранее подготовленный объект перевода
            return self._translation.translate(text)
        except Exception as e:
            log.error("Ошибка локального перевода: %s", e)
            return text