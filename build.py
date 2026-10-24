import PyInstaller.__main__

APP_NAME = "SyncLingua-Server"

PyInstaller.__main__.run([
    'server.py',
    '--name=%s' % APP_NAME,
    '--onedir',
    '--noconfirm',
    '--clean',
    
    # Добавляем папки с моделями
    '--add-data=piper_models;piper_models',
    '--add-data=argos_models;argos_models',
    
    # ВОТ ЭТА СТРОЧКА РЕШИТ ПРОБЛЕМУ:
    # Она говорит PyInstaller'у скопировать все файлы данных (включая .onnx)
    '--collect-all=faster_whisper',
    '--collect-all=ctranslate2', 
    '--collect-all=onnxruntime',
    
    # Скрытые импорты для Uvicorn
    '--hidden-import=uvicorn.logging',
    '--hidden-import=uvicorn.loops',
    '--hidden-import=uvicorn.loops.auto',
    '--hidden-import=uvicorn.protocols',
    '--hidden-import=uvicorn.protocols.http',
    '--hidden-import=uvicorn.protocols.http.auto',
    '--hidden-import=uvicorn.protocols.websockets',
    '--hidden-import=uvicorn.protocols.websockets.auto',
    '--hidden-import=uvicorn.lifespan',
    '--hidden-import=uvicorn.lifespan.on',
    '--hidden-import=piper',
    '--hidden-import=argostranslate',
])

print("Сборка завершена! Ищите папку dist/SyncLingua-Server")