import PyInstaller.__main__

APP_NAME = "SyncLingua-Server"

PyInstaller.__main__.run([
    'server.py',
    '--name=%s' % APP_NAME,
    '--onedir',
    '--noconfirm',
    '--clean',
    
    # Добавляем папки с моделями Argos
    '--add-data=argos_models;argos_models',
    
    # Явно копируем espeak-ng-data внутрь папки piper
    '--add-data=.venv_piper/Lib/site-packages/piper/espeak-ng-data;piper/espeak-ng-data',
    
    # Собираем все данные для Piper
    '--collect-all=piper',
    '--collect-all=faster_whisper',
    
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
    '--hidden-import=argostranslate',
    '--hidden-import=ctranslate2',
])

print("Сборка завершена! Ищите папку dist/SyncLingua-Server")