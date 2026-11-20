import logging
import time
import requests
from .base import TranslationAdapter
from .openai_translation import TranslationError

log = logging.getLogger(__name__)

class FallbackTranslationAdapter(TranslationAdapter):
    """Обёртка: primary → fallback при soft-ошибке + circuit breaker."""

    def __init__(self, primary: TranslationAdapter, fallback: TranslationAdapter,
                 failure_threshold: int = 3, cooldown_sec: float = 30.0) -> None:
        self._primary = primary
        self._fallback = fallback
        self._threshold = failure_threshold
        self._cooldown = cooldown_sec
        self._failures = 0
        self._open_until = 0.0
        self._pending_message: dict | None = None
        self._using_fallback = False

    def take_pending_message(self) -> dict | None:
        msg, self._pending_message = self._pending_message, None
        return msg

    def _circuit_open(self) -> bool:
        if time.monotonic() < self._open_until:
            return True
        if self._open_until > 0:
            self._open_until = 0.0
            self._failures = 0
            log.info("Fallback: cooldown истёк, пробуем вернуться к primary")
        return False

    @staticmethod
    def _is_soft_error(exc: Exception) -> bool:
        if isinstance(exc, TranslationError):
            return not exc.fatal
        return isinstance(exc, (requests.Timeout, requests.ConnectionError))

    def translate(self, text: str) -> str:
        if self._circuit_open():
            return self._fallback.translate(text)

        try:
            result = self._primary.translate(text)
        except Exception as exc:
            if not self._is_soft_error(exc):
                raise
            self._failures += 1
            log.warning("Fallback: primary упал (%s), %d/%d", exc, self._failures, self._threshold)

            if self._failures >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown
                log.warning("Fallback: circuit открыт на %.0f с", self._cooldown)

            if not self._using_fallback:
                self._using_fallback = True
                self._pending_message = {
                    "type": "info",
                    "message": "Облако недоступно, переключились на локальный перевод",
                }
            return self._fallback.translate(text)

        if self._failures > 0:
            self._failures = 0
            log.info("Fallback: primary восстановился")
        if self._using_fallback:
            self._using_fallback = False
            self._pending_message = {
                "type": "info",
                "message": "Облачный перевод восстановлен",
            }
        return result

    def warmup(self) -> None:
        if hasattr(self._fallback, "warmup"):
            self._fallback.warmup()

    def close(self) -> None:
        for a in (self._primary, self._fallback):
            if hasattr(a, "close"):
                try:
                    a.close()
                except Exception:
                    pass