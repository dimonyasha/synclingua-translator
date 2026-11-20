[Setup]
AppName=SyncLingua
AppVersion=1.2.0
AppPublisher=Dimonyasha
AppPublisherURL=https://github.com/dimonyasha/synclingua-translator
AppSupportURL=https://github.com/dimonyasha/synclingua-translator
AppUpdatesURL=https://github.com/dimonyasha/synclingua-translator
DefaultDirName={autopf}\SyncLingua
DisableProgramGroupPage=yes
; Раскомментируй строку ниже, если хочешь выбрать иконку для установщика (нужно будет положить icon.ico рядом)
SetupIconFile=icon.ico
OutputDir=Output
OutputBaseFilename=SyncLingua_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; Требуем права администратора (чтобы писать в Program Files)
PrivilegesRequired=admin
; Минимальная версия Windows
MinVersion=10.0

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; ВНИМАНИЕ: Путь к папке SyncLingua_Install должен быть абсолютным или относительным.
; Если папка лежит рядом со скриптом, оставь так, как написано ниже.
Source: "C:\Users\Димон\Desktop\SyncLingua_Install\SyncLingua-Server\*"; DestDir: "{app}\SyncLingua-Server"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "C:\Users\Димон\Desktop\SyncLingua_Install\chrome-translator\*"; DestDir: "{app}\chrome-translator"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "C:\Users\Димон\Desktop\SyncLingua_Install\README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\SyncLingua Server"; Filename: "{app}\SyncLingua-Server\SyncLingua-Server.exe"
Name: "{autodesktop}\SyncLingua Server"; Filename: "{app}\SyncLingua-Server\SyncLingua-Server.exe"; Tasks: desktopicon

[Run]
; После установки открываем README в блокноте (чтобы пользователь знал, что делать)
Filename: "{app}\README.txt"; Description: "Открыть инструкцию (README.txt)"; Flags: postinstall shellexec
; Предлагаем скачать Ollama (открываем браузер)
Filename: "https://ollama.com/download"; Description: "Скачать Ollama (обязательно для локального режима)"; Flags: postinstall shellexec unchecked