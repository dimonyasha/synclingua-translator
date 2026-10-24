import argparse
import logging
import sys
import os
os.environ["ARGOS_PACKAGES_DIR"] = r"z:\Projects\LiveVoice\audio_translator\argos_models"
from pathlib import Path
from adapters import EdgeTTSAdapter, FasterWhisperASR, GoogleTranslationAdapter
from adapters.tts import DEFAULT_VOICE
from pipeline import TranslationPipeline

import requests
from huggingface_hub import set_client_factory

def create_client_without_ssl_verify():
    session = requests.Session()
    session.verify = False
    return session

set_client_factory(create_client_without_ssl_verify)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EN audio -> RU audio (PoC)")
    p.add_argument("audio", type=Path, help="Путь к аудиофайлу")
    p.add_argument("--model", default="base", help="Размер модели Whisper (tiny/base/small)")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--save-text", action="store_true")
    return p.parse_args()

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not args.audio.is_file():
        print(f"Файл не найден: {args.audio}", file=sys.stderr)
        return 1

    pipeline = TranslationPipeline(
        asr=FasterWhisperASR(model_size=args.model, device=args.device),
       translator=GoogleTranslationAdapter(source="en", target="ru"),
        tts=EdgeTTSAdapter(voice=args.voice),
    )
    try:
        result, _ = pipeline.run(args.audio, args.output, save_text=args.save_text)
    except Exception as exc:
        logging.error("Ошибка пайплайна: %s", exc)
        return 2
    print(f"\nГотово: {result}")
    return 0

if __name__ == "__main__":
    sys.exit(main())