// --- Объявления всех элементов (сначала!) ---
const autoFallback = document.getElementById('auto-fallback');
const transMode = document.getElementById('trans-mode');
const cloudSettings = document.getElementById('cloud-settings');
const cloudUrl = document.getElementById('cloud-url');
const cloudKey = document.getElementById('cloud-key');
const cloudModel = document.getElementById('cloud-model');
const startBtn = document.getElementById('start');
const stopBtn = document.getElementById('stop');
let currentTabId = null;
chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
    if (tab) currentTabId = tab.id;
});
const statusEl = document.getElementById('status');
const transVol = document.getElementById('trans-vol');
const origVol = document.getElementById('orig-vol');
const transVolVal = document.getElementById('trans-vol-val');
const origVolVal = document.getElementById('orig-vol-val');
const voiceSelect = document.getElementById('voice-select');
const showSubs = document.getElementById('show-subs');
const subSize = document.getElementById('sub-size');
const xttsVoiceSelect = document.getElementById('xtts-voice-select'); // <-- для XTTS/F5
const voiceBlock = document.getElementById('voice-block'); // <-- блок XTTS
const chunkSeconds = document.getElementById('chunk-seconds');
const chunkVal = document.getElementById('chunk-val');
const keepAlive = document.getElementById('keep-alive');
const cloudVerifySsl = document.getElementById('cloud-verify-ssl');
const subtitleOnly = document.getElementById('subtitle-only');

const STATUS_TEXT = {
  idle: 'Выключено', starting: 'Запуск…', running: 'Работает: слушаю вкладку',
  reconnecting: 'Нет связи с сервером, переподключение…', error: 'Ошибка',
};

function render(state = { active: false, status: 'idle', detail: '', tabId: null }) {
  const isCurrentTab = currentTabId && state.tabId === currentTabId;
  
  startBtn.disabled = state.active && isCurrentTab;
  stopBtn.disabled = !state.active; 
  
  statusEl.className = state.status === 'error' ? 'error' : '';
  
  if (state.active && !isCurrentTab) {
    statusEl.textContent = 'Работает на другой вкладке';
  } else {
    statusEl.textContent = [STATUS_TEXT[state.status] ?? state.status, state.detail]
      .filter(Boolean).join(' — ');
  }
}

// --- Восстановление настроек ---
chrome.storage.local.get(['transVol', 'origVol', 'showSubs', 'subSize', 'chunkSeconds', 'keepAlive', 'transMode', 'cloudUrl', 'cloudKey', 'cloudModel', 'cloudVerifySsl', 'subtitleOnly', 'autoFallback'], (res) => {
  autoFallback.checked = res.autoFallback ?? false;
  transVol.value = res.transVol ?? 100;
  origVol.value = res.origVol ?? 100;
  showSubs.checked = res.showSubs ?? true;
  subSize.value = res.subSize ?? '24';
  chunkSeconds.value = res.chunkSeconds ?? 4.0;
  chunkVal.textContent = (res.chunkSeconds ?? 4.0).toFixed(1);
  keepAlive.checked = res.keepAlive ?? true;
  subtitleOnly.checked = res.subtitleOnly ?? false;
  
  // Загружаем настройки перевода
  transMode.value = res.transMode ?? 'local';
  cloudUrl.value = res.cloudUrl ?? '';
  cloudKey.value = res.cloudKey ?? '';
  cloudModel.value = res.cloudModel ?? '';
  cloudVerifySsl.checked = res.cloudVerifySsl ?? true;
  cloudSettings.style.display = transMode.value === 'cloud' ? 'block' : 'none';
  
  transVolVal.textContent = `${transVol.value}%`;
  origVolVal.textContent = `${origVol.value}%`;
});

chrome.storage.session.get('state').then(({ state }) => render(state));

// Обновляем при смене вкладки, пока попап открыт
chrome.tabs.onActivated.addListener(async () => {
    const { state } = await chrome.storage.session.get('state');
    render(state);
});

// --- Обработчики ---
function sendVolumeSettings() {
  const t = transVol.value;
  const o = origVol.value;
  transVolVal.textContent = `${t}%`;
  origVolVal.textContent = `${o}%`;
  chrome.storage.local.set({ transVol: t, origVol: o });
  chrome.runtime.sendMessage({ target: 'background', type: 'set-volume', transVol: t / 100, origVol: o / 100 });
}

transVol.addEventListener('input', sendVolumeSettings);
origVol.addEventListener('input', sendVolumeSettings);

showSubs.addEventListener('change', () => {
  chrome.storage.local.set({ showSubs: showSubs.checked });
  chrome.runtime.sendMessage({ target: 'background', type: 'update-sub-settings', show: showSubs.checked, size: subSize.value });
});

subSize.addEventListener('change', () => {
  chrome.storage.local.set({ subSize: subSize.value });
  chrome.runtime.sendMessage({ target: 'background', type: 'update-sub-settings', show: showSubs.checked, size: subSize.value });
});

chunkSeconds.addEventListener('input', () => {
  const val = parseFloat(chunkSeconds.value);
  chunkVal.textContent = val.toFixed(1);
  chrome.storage.local.set({ chunkSeconds: val });
  chrome.runtime.sendMessage({ target: 'background', type: 'set-chunk', value: val });
});

keepAlive.addEventListener('change', () => {
  chrome.storage.local.set({ keepAlive: keepAlive.checked });
  chrome.runtime.sendMessage({ target: 'background', type: 'set-keep-alive', value: keepAlive.checked });
});

// --- Обработчик кнопки "Включить" ---
startBtn.addEventListener('click', async () => {
  startBtn.disabled = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw new Error('Не найдена активная вкладка');
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    
    // Принудительно сохраняем галочки перед стартом
    await chrome.storage.local.set({ 
        subtitleOnly: subtitleOnly.checked,
        autoFallback: autoFallback.checked 
    });
    
    const res = await chrome.runtime.sendMessage({ target: 'background', type: 'start', tabId: tab.id, streamId });
    if (!res?.ok) throw new Error(res?.error || 'Не удалось запустить');
    
    sendVolumeSettings();
    
  } catch (err) {
    render({ active: false, status: 'error', detail: err.message });
  }
});

stopBtn.addEventListener('click', async () => {
  stopBtn.disabled = true;
  await chrome.runtime.sendMessage({ target: 'background', type: 'stop' });
});

// --- Голоса XTTS/F5 ---
const SERVER_HTTP = 'http://127.0.0.1:8000';

async function loadVoices() {
  const piperBlock = document.getElementById('piper-voice-block');
  try {
    const res = await fetch(`${SERVER_HTTP}/voices`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const info = await res.json();

    // Если движок XTTS или F5 — показываем блок XTTS, скрываем Piper
    if (info.engine === 'xtts' || info.engine === 'f5') {
      if (!info.voices?.length) {
        voiceBlock.hidden = true;
        return;
      }
      xttsVoiceSelect.replaceChildren(...info.voices.map((v) => new Option(v.label, v.file)));
      const { xttsVoice } = await chrome.storage.local.get('xttsVoice');
      const files = info.voices.map((v) => v.file);
      xttsVoiceSelect.value = files.includes(xttsVoice) ? xttsVoice : info.voice;
      voiceBlock.hidden = false;
      if (piperBlock) piperBlock.hidden = true;
      return;
    }

    // Если движок Piper — показываем блок Piper, скрываем XTTS
    if (info.engine === 'piper') {
      voiceBlock.hidden = true;
      if (piperBlock) {
        piperBlock.hidden = false;
        // Восстанавливаем выбранный голос
        const { voice } = await chrome.storage.local.get('voice');
        voiceSelect.value = voice ?? 'male_1';
      }
      return;
    }

    // Неизвестный движок — прячем оба
    voiceBlock.hidden = true;
    if (piperBlock) piperBlock.hidden = true;
  } catch {
    voiceBlock.hidden = true;
    if (piperBlock) piperBlock.hidden = true;
  }
}

xttsVoiceSelect.addEventListener('change', () => {
  chrome.runtime.sendMessage({
    target: 'background', 
    type: 'set-voice', 
    voice: xttsVoiceSelect.value
  }).catch(() => {});
});

// Старый селект для Piper (можно оставить, он всё равно скрыт)
if (voiceSelect) {
  voiceSelect.addEventListener('change', () => {
    const v = voiceSelect.value;
    chrome.storage.local.set({ voice: v });
    chrome.runtime.sendMessage({ target: 'background', type: 'set-voice', voice: v });
  });
}
// --- СОХРАНЕНИЕ НАСТРОЕК ОБЛАКА ---
function saveTranslationSettings() {
  chrome.storage.local.set({
	transMode: transMode.value,
    cloudUrl: cloudUrl.value.trim(),
    cloudKey: cloudKey.value.trim(),
    cloudModel: cloudModel.value.trim(),
    cloudVerifySsl: cloudVerifySsl.checked,
    autoFallback: autoFallback.checked	
  });
}

transMode.addEventListener('change', () => {
  cloudSettings.style.display = transMode.value === 'cloud' ? 'block' : 'none';
  saveTranslationSettings();
  
  // ДОБАВИТЬ ЭТО:
  const mode = transMode.value;
  chrome.runtime.sendMessage({
    target: 'background',
    type: 'update-translation-mode',
    data: mode === 'cloud' ? {
        mode: 'cloud', base_url: cloudUrl.value, api_key: cloudKey.value, model: cloudModel.value, verify_ssl: cloudVerifySsl.checked, auto_fallback: autoFallback.checked
    } : { mode: 'local' }
  });
});

// ВОТ ЗДЕСЬ ИСПОЛЬЗУЕМ 'input' ВМЕСТО 'change':
// Сохраняем текстовые поля при вводе
[cloudUrl, cloudKey, cloudModel].forEach(el => 
  el.addEventListener('input', saveTranslationSettings)
);

// Сохраняем галочку SSL при изменении
cloudVerifySsl.addEventListener('change', saveTranslationSettings);

subtitleOnly.addEventListener('change', () => {
  chrome.storage.local.set({ subtitleOnly: subtitleOnly.checked });
  chrome.runtime.sendMessage({ target: 'background', type: 'set-subtitle-only', value: subtitleOnly.checked });
});

// Слушаем прямые сообщения от background.js о смене статуса
chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.target === 'popup' && msg.type === 'state-changed') {
        render(msg.state);
    }
});

autoFallback.addEventListener('change', saveTranslationSettings);

loadVoices();

// Слушаем сообщение о падении облака
chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.target === 'popup' && msg.type === 'fallback-triggered') {
        const warningDiv = document.getElementById('fallback-warning');
        const warningText = document.getElementById('fallback-text');
        
        warningText.textContent = msg.message;
        warningDiv.style.display = 'block';
        
        // Через 5 секунд прячем плашку, чтобы не мешала
        setTimeout(() => {
            warningDiv.style.display = 'none';
        }, 5000);
    }
});