from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

class ASRAdapter(ABC):
    @abstractmethod
    def transcribe(self, audio_path: Path) -> list[Segment]:
        ...

class TranslationAdapter(ABC):
    @abstractmethod
    def translate(self, text: str) -> str:
        ...

class TTSAdapter(ABC):
    @abstractmethod
    def synthesize(self, text: str, output_path: Path) -> Path:
        ...