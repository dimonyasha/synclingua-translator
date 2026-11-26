import os
import sys
import ollama
import ctranslate2
import subprocess
import urllib.request
import time
import shutil

OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_EXE = "OllamaSetup.exe"

def is_ollama_installed():
    """Проверяем, установлена ли Ollama в системе"""
    try:
        # Проверяем, отвечает ли локальный сервер Ollama
        import httpx
        response = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2)
        return response.status_code == 200
    except Exception:
        return False

def download_file(url, filename):
    """Скачиваем файл с прогресс-баром"""
    print(f"Скачивание {filename}...")
    def reporthook(count, block_size, total_size):
        if total_size > 0:
            percent = int(count * block_size * 100 / total_size)
            sys.stdout.write(f"\rЗагрузка: {percent}%")
            sys.stdout.flush()
    
    urllib.request.urlretrieve(url, filename, reporthook)
    print("\nСкачивание завершено.")

def install_ollama():
    if not os.path.exists(OLLAMA_EXE):
        download_file(OLLAMA_URL, OLLAMA_EXE)
    
    print("Установка Ollama (тихий режим)...")
    try:
        # Запускаем установщик Ollama в тихом режиме
        subprocess.run([OLLAMA_EXE, "/SILENT"], check=True)
        print("Ollama успешно установлена.")
        # Удаляем установщик после установки
        os.remove(OLLAMA_EXE)
    except Exception as e:
        print(f"Ошибка при установке Ollama: {e}")
        sys.exit(1)

def pull_model(model_name="translategemma:4b"):
    print(f"\nПроверка модели {model_name}...")
    client = ollama.Client()
    try:
        # Пытаемся получить список установленных моделей
        installed = [m['name'] for m in client.list()['models']]
        # Если модель есть (с учетом тега :latest), пропускаем
        if any(model_name in m for m in installed):
            print(f"Модель {model_name} уже установлена.")
            return
    except Exception:
        pass # Если Ollama еще не готова, просто идем качать

    print(f"Скачивание модели {model_name} (это может занять несколько минут)...")
    try:
        # Скачиваем с прогресс-баром
        for progress in client.pull(model_name, stream=True):
            status = progress.get('status', '')
            completed = progress.get('completed')
            total = progress.get('total')
            if completed is not None and total is not None and total > 0:
                percent = int(completed * 100 / total)
                sys.stdout.write(f"\r{status[:20]:<20} {percent}%")
                sys.stdout.flush()
            else:
                sys.stdout.write(f"\r{status[:20]:<20} ")
                sys.stdout.flush()
        print("\nСкачивание модели завершено.")
    except Exception as e:
        print(f"\nОшибка при скачивании модели: {e}")

def check_hardware():
    print("\nАнализ аппаратного обеспечения...")
    try:
        # Запрашиваем объем памяти у драйвера NVIDIA
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True
        )
        # Парсим ответ (в мегабайтах)
        vram_mb = int(result.stdout.strip().split('\n')[0])
        vram_gb = vram_mb / 1024
        
        print(f"Найдена видеокарта NVIDIA. Объем видеопамяти (VRAM): {vram_gb:.1f} ГБ")
        
        if vram_gb > 6.0:
            print("Рекомендуется модель Whisper 'large-v3-turbo' (высокое качество).")
            return "large-v3-turbo"
        else:
            print("Рекомендуется модель Whisper 'small' (для экономии VRAM).")
            return "small"
            
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Видеокарта NVIDIA не найдена или драйверы не установлены.")
        print("Будет использоваться CPU и модель 'small'.")
        return "small"

def main():
    print("=== SyncLingua Setup Assistant ===")
    
    # Шаг 1: Проверка Ollama
    if is_ollama_installed():
        print("Ollama уже установлена и запущена.")
    else:
        print("Ollama не найдена. Начинаем автоматическую установку...")
        install_ollama()
        print("Ждем 8 секунд, пока Ollama запустится в фоне...")
        time.sleep(8)
    
    # Шаг 2: Скачивание модели перевода
    pull_model("translategemma:4b")
    
    # Шаг 3: Определение железа
    recommended_asr = check_hardware()
    
    # Сохраняем выбор в конфиг (сервер потом его прочитает)
    print("\nСохранение настроек...")
    with open("config.ini", "w", encoding="utf-8") as f:
        f.write("[Settings]\n")
        f.write(f"asr_model={recommended_asr}\n")
    print("Настройки сохранены в config.ini")
    
    print("\n=== Настройка завершена! ===")
    print("Теперь вы можете запустить сервер SyncLingua-Server.exe")

if __name__ == "__main__":
    main()