// Service worker: создаёт/закрывает offscreen-документ и хранит состояние.
// Сам звук не трогает: в service worker нет Web Audio API.

const OFFSCREEN_PATH = 'offscreen.html';
const IDLE = { active: false, status: 'idle', detail: '', tabId: null };

// ---- состояние (переживает «засыпание» service worker) ----
async function getState() {
  const { state } = await chrome.storage.session.get('state');
  return state ?? IDLE;
}
async function setState(patch) {
  const next = { ...(await getState()), ...patch };
  await chrome.storage.session.set({ state: next });
  return next;
}

// ---- offscreen-документ ----
async function hasOffscreen() {
  const ctxs = await chrome.runtime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT'],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN_PATH)],
  });
  return ctxs.length > 0;
}

async function ensureOffscreen() {
  if (await hasOffscreen()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_PATH,
    // USER_MEDIA: getUserMedia с tab-стримом; AUDIO_PLAYBACK: звук оригинала и перевода.
    reasons: ['USER_MEDIA', 'AUDIO_PLAYBACK'],
    justification: 'Захват звука вкладки, обработка в AudioWorklet и воспроизведение перевода',
  });
}

// Небольшие ретраи: страница может ещё не успеть повесить onMessage
async function sendToOffscreen(message, attempts = 5) {
  for (let i = 0; i < attempts; i++) {
    try {
      return await chrome.runtime.sendMessage({ target: 'offscreen', ...message });
    } catch (err) {
      if (i === attempts - 1) throw err;
      await new Promise((r) => setTimeout(r, 150));
    }
  }
}

// ---- команды ----
async function startTranslation({ tabId, streamId }) {
  if ((await getState()).active) await stopTranslation();
  await setState({ active: true, status: 'starting', detail: '', tabId });
  try {
    await ensureOffscreen();
    const res = await sendToOffscreen({ type: 'start-capture', streamId });
    if (!res?.ok) throw new Error(res?.error || 'Offscreen-документ не запустил захват');
    return { ok: true };
  } catch (err) {
    await stopTranslation();
    await setState({ status: 'error', detail: err.message });
    return { ok: false, error: err.message };
  }
}

async function stopTranslation() {
  if (await hasOffscreen()) {
    try { await sendToOffscreen({ type: 'stop-capture' }, 1); } catch { /* уже закрыт */ }
    await chrome.offscreen.closeDocument().catch(() => {});
  }
  await setState(IDLE);
}

// ---- сообщения ----
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== 'background') return false; // чужие сообщения не трогаем

  (async () => {
    switch (msg.type) {
      case 'start':
        return startTranslation(msg);
      case 'stop':
        await stopTranslation();
        return { ok: true };
      case 'capture-ended': 
        await stopTranslation();
        return { ok: true };
      case 'status': 
        if ((await getState()).active) {
          await setState({ status: msg.status, detail: msg.detail ?? '' });
        }
        return { ok: true };
      case 'set-volume':
        await sendToOffscreen({ type: 'set-volume', transVol: msg.transVol, origVol: msg.origVol });
        return { ok: true };
      case 'set-voice':
        await sendToOffscreen({ type: 'set-voice', voice: msg.voice });
        return { ok: true };
      case 'update-sub-settings':
        // Отправляем настройки на активную вкладку в content.js
        const currentState = await getState();
        if (currentState.active && currentState.tabId) {
          chrome.tabs.sendMessage(currentState.tabId, { 
            target: 'content', 
            type: 'update-sub-settings', 
            show: msg.show, 
            size: msg.size 
          }).catch(() => {});
        }
        return { ok: true };
        
      // --- ВОТ СЮДА ДОБАВЛЯЕМ НОВЫЙ КЕЙС ---
      case 'show-subtitle':
        const state = await getState();
        if (state.active && state.tabId) {
            chrome.tabs.sendMessage(state.tabId, { target: 'content', type: 'subtitle', text: msg.text }).catch(() => {});
        }
        return { ok: true };
      // -------------------------------------

      default:
        return { ok: false, error: `unknown type ${msg.type}` };
    }
  })()
    .then(sendResponse)
    .catch((err) => sendResponse({ ok: false, error: String(err?.message ?? err) }));

  return true; // ответ асинхронный
});

// Автоматически останавливаем захват, если пользователь переключил вкладку
chrome.tabs.onActivated.addListener(async (activeInfo) => {
  const state = await getState();
  // Если перевод включен, и мы ушли на другую вкладку — выключаем
  if (state.active && state.tabId !== activeInfo.tabId) {
    console.log("Смена вкладки: останавливаем перевод, чтобы освободить захват.");
    await stopTranslation();
  }
});