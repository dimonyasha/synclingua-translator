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

// Восстанавливаем настройки при открытии
chrome.storage.local.get(['transVol', 'origVol', 'voice', 'showSubs', 'subSize'], (res) => {
  transVol.value = res.transVol ?? 100;
  origVol.value = res.origVol ?? 100;
  voiceSelect.value = res.voice ?? 'male_1';
  showSubs.checked = res.showSubs ?? true;
  subSize.value = res.subSize ?? '24';
  
  transVolVal.textContent = `${transVol.value}%`;
  origVolVal.textContent = `${origVol.value}%`;
});

chrome.storage.session.get('state').then(({ state }) => render(state));
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.state) render(changes.state.newValue);
});

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

voiceSelect.addEventListener('change', () => {
  const v = voiceSelect.value;
  chrome.storage.local.set({ voice: v });
  chrome.runtime.sendMessage({ target: 'background', type: 'set-voice', voice: v });
});

// Отправка настроек субтитров
function sendSubtitleSettings() {
  const isVisible = showSubs.checked;
  const size = subSize.value;
  chrome.storage.local.set({ showSubs: isVisible, subSize: size });
  chrome.runtime.sendMessage({ target: 'background', type: 'update-sub-settings', show: isVisible, size: size });
}

showSubs.addEventListener('change', sendSubtitleSettings);
subSize.addEventListener('change', sendSubtitleSettings);

startBtn.addEventListener('click', async () => {
  startBtn.disabled = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw new Error('Не найдена активная вкладка');
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    
    const res = await chrome.runtime.sendMessage({ target: 'background', type: 'start', tabId: tab.id, streamId });
    if (!res?.ok) throw new Error(res?.error || 'Не удалось запустить');
    
    sendVolumeSettings();
    sendSubtitleSettings();
    chrome.runtime.sendMessage({ target: 'background', type: 'set-voice', voice: voiceSelect.value });
    
  } catch (err) {
    render({ active: false, status: 'error', detail: err.message });
  }
});

stopBtn.addEventListener('click', async () => {
  stopBtn.disabled = true;
  await chrome.runtime.sendMessage({ target: 'background', type: 'stop' });
});