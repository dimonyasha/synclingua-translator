// --- Объявления всех элементов (сначала!) ---
const startBtn = document.getElementById('start');
const stopBtn = document.getElementById('stop');
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

const STATUS_TEXT = {
  idle: 'Выключено', starting: 'Запуск…', running: 'Работает: слушаю вкладку',
  reconnecting: 'Нет связи с сервером, переподключение…', error: 'Ошибка',
};

function render(state = { active: false, status: 'idle', detail: '' }) {
  startBtn.disabled = state.active;
  stopBtn.disabled = !state.active;
  statusEl.className = state.status === 'error' ? 'error' : '';
  statusEl.textContent = [STATUS_TEXT[state.status] ?? state.status, state.detail]
    .filter(Boolean).join(' — ');
}

// --- Восстановление настроек ---
chrome.storage.local.get(['transVol', 'origVol', 'showSubs', 'subSize', 'chunkSeconds', 'keepAlive'], (res) => {
  transVol.value = res.transVol ?? 100;
  origVol.value = res.origVol ?? 100;
  showSubs.checked = res.showSubs ?? true;
  subSize.value = res.subSize ?? '24';
  chunkSeconds.value = res.chunkSeconds ?? 4.0;
  chunkVal.textContent = (res.chunkSeconds ?? 4.0).toFixed(1);
  keepAlive.checked = res.keepAlive ?? true;
  
  transVolVal.textContent = `${transVol.value}%`;
  origVolVal.textContent = `${origVol.value}%`;
});

chrome.storage.session.get('state').then(({ state }) => render(state));
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.state) render(changes.state.newValue);
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

loadVoices();