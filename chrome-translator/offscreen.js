// Offscreen-документ: тут живёт весь звук.
//
//   вкладка ─ getUserMedia ─► MediaStreamSource ─┬─► originalGain ─► динамики (оригинал)
//                                                └─► AudioWorklet (16 кГц PCM16) ─► WebSocket
//   WebSocket ─► mp3 (base64) ─► decodeAudioData ─► translationGain ─► динамики (перевод)

const WS_URL = 'ws://127.0.0.1:8000/ws/translate';
const TARGET_RATE = 16000;     // то, что ждёт сервер
const CHUNK_MS = 160;          // размер чанка: 2560 сэмплов = 5120 байт
const ORIGINAL_GAIN = 1.0;     // сюда позже добавим приглушение оригинала (ducking)
const TRANSLATION_GAIN = 1.0;
const MAX_BUFFERED = 1_000_000; // если сокет «забит» больше 1 МБ, чанки отбрасываем

let ctx = null;
let stream = null;
let workletNode = null;
let originalGain = null;
let translationGain = null;
let ws = null;
let preferredVoice = null; // Голос, который выбрал пользователь
let serverEngine = null;   // 'xtts' или 'piper'
let serverVoice = null;    // Голос, который сейчас активен на сервере
let active = false;
let preferredChunkSeconds = 4.0;
let reconnectTimer = null;
let reconnectAttempts = 0;
let nextPlayTime = 0;          // когда закончится последний запланированный кусок перевода
let playChain = Promise.resolve(); // сохраняет порядок воспроизведения
let currentTransVol = 1.0;
let currentOrigVol = 1.0;

// ------------------------------------------------------------ связь с background
function report(status, detail = '') {
  chrome.runtime
    .sendMessage({ target: 'background', type: 'status', status, detail })
    .catch(() => {});
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== 'offscreen') return false;

  (async () => {
    try {
      if (msg.type === 'start-capture') {
        preferredVoice = msg.voice ?? null;
        preferredChunkSeconds = msg.chunkSeconds ?? 4.0;
        await startCapture(msg.streamId);
      } 
      else if (msg.type === 'stop-capture') await stopCapture();
      else if (msg.type === 'set-voice') {
        preferredVoice = msg.voice;
        applyPreferredVoice();
      }
      else if (msg.type === 'set-chunk') {
        preferredChunkSeconds = msg.value;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'set-chunk', value: preferredChunkSeconds }));
        }
      }
      else if (msg.type === 'set-volume') {
        currentTransVol = msg.transVol;
        currentOrigVol = msg.origVol;
        
        // Используем setTargetAtTime для плавного применения, чтобы не было щелчков
        if (originalGain) {
            originalGain.gain.setTargetAtTime(currentOrigVol, ctx.currentTime, 0.05);
        }
        if (translationGain) {
            translationGain.gain.setTargetAtTime(currentTransVol, ctx.currentTime, 0.05);
        }
      }
      else if (msg.type === 'set-voice') {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'set-voice', voice: msg.voice }));
        }
      }
      
      sendResponse({ ok: true });
      
    } catch (err) {
      console.error('[offscreen]', err);
      await stopCapture();
      sendResponse({ ok: false, error: String(err?.message ?? err) });
    }
  })();
  
  return true;
});

// ------------------------------------------------------------ захват
async function startCapture(streamId) {
  if (active) await stopCapture();

  // Стрим вкладки по streamId, полученному в popup
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId },
    },
    video: false,
  });

  // Вкладка закрылась / захват оборвался -> просим background всё остановить
  stream.getAudioTracks()[0]?.addEventListener('ended', () => {
    chrome.runtime.sendMessage({ target: 'background', type: 'capture-ended' }).catch(() => {});
  });

  ctx = new AudioContext(); // родная частота устройства: оригинал играет без потери качества
  await ctx.resume();
  await ctx.audioWorklet.addModule('audio-processor.js');

  const source = ctx.createMediaStreamSource(stream);

  // 1) Оригинал. tabCapture глушит вкладку для пользователя, поэтому
  //    возвращаем звук в динамики сами.
  originalGain = ctx.createGain();
  originalGain.gain.value = ORIGINAL_GAIN;
  source.connect(originalGain).connect(ctx.destination);

  // 2) Перевод идёт в тот же destination (микшируется с оригиналом)
  translationGain = ctx.createGain();
  translationGain.gain.value = TRANSLATION_GAIN;
  translationGain.connect(ctx.destination);

  // 3) Ветка для сервера: downmix в моно (channelCountMode: 'explicit') -> worklet
  workletNode = new AudioWorkletNode(ctx, 'pcm16-processor', {
    channelCount: 1,
    channelCountMode: 'explicit',
    processorOptions: { targetRate: TARGET_RATE, chunkMs: CHUNK_MS },
  });
  workletNode.port.onmessage = (e) => sendChunk(e.data);
  source.connect(workletNode);
  // Выход worklet тихий (мы в него ничего не пишем); подключение к destination
  // гарантирует, что браузер не «выключит» ноду как неиспользуемую.
  workletNode.connect(ctx.destination);

  active = true;
  nextPlayTime = 0;
  reconnectAttempts = 0;
  connectWs();
}

async function stopCapture() {
  active = false;
  clearTimeout(reconnectTimer);
  serverEngine = serverVoice = null;
  if (ws) {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try { ws.close(); } catch { /* ignore */ }
    ws = null;
  }
  if (workletNode) {
    workletNode.port.onmessage = null;
    workletNode.disconnect();
    workletNode = null;
  }
  stream?.getTracks().forEach((t) => t.stop()); // звук вкладки возвращается в норму
  stream = null;
  if (ctx && ctx.state !== 'closed') await ctx.close().catch(() => {});
  ctx = originalGain = translationGain = null;
  playChain = Promise.resolve();
}

// ------------------------------------------------------------ WebSocket
function connectWs() {
  const sock = new WebSocket(WS_URL);
  ws = sock;

    sock.onopen = () => {
      if (sock !== ws) return;
      reconnectAttempts = 0;
      sock.send(JSON.stringify({
        type: 'config', format: 'pcm16', sample_rate: TARGET_RATE, channels: 1,
      }));
      // Отправляем сохранённую скорость чанка
      sock.send(JSON.stringify({ type: 'set-chunk', value: preferredChunkSeconds }));
      report('running');
    };

  sock.onmessage = (ev) => {
    if (sock === ws) handleServerMessage(ev.data);
  };

  sock.onerror = () => { /* подробности придут в onclose */ };

  sock.onclose = () => {
    if (sock !== ws || !active) return;
    // Экспоненциальная пауза: 1, 2, 4, 8, 10, 10... секунд
    const delay = Math.min(1000 * 2 ** reconnectAttempts++, 10000);
    report('reconnecting', `повтор через ${Math.round(delay / 1000)} с`);
    reconnectTimer = setTimeout(() => active && connectWs(), delay);
  };
}

function sendChunk(buffer) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return; // нет связи: чанк теряем
  if (ws.bufferedAmount > MAX_BUFFERED) return;         // сеть не успевает: тоже теряем
  ws.send(JSON.stringify({ type: 'audio', data: bytesToBase64(new Uint8Array(buffer)) }));
}

// Отправляем голос на сервер, если он изменился
function applyPreferredVoice() {
  if (!preferredVoice) return;
  if (preferredVoice === serverVoice) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  
  // Всегда отправляем set-xtts-voice, сервер обработает его для любого движка
  ws.send(JSON.stringify({ type: 'set-xtts-voice', voice: preferredVoice }));
  console.log(`[offscreen] Отправлена смена голоса: ${preferredVoice}`);
}

function handleServerMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return;
  }
  switch (msg.type) {
    case 'audio':
      playChain = playChain.then(() => playMp3(msg.data, msg.seq));
      if (msg.text) {
          chrome.runtime.sendMessage({ target: 'background', type: 'show-subtitle', text: msg.text });
      }
      break;
    case 'error':
      console.warn('[server error]', msg.message);
      report('running', `Сервер: ${msg.message}`);
      break;
    case 'ready':
      serverEngine = msg.engine ?? null;
      serverVoice = msg.voice ?? null;
      applyPreferredVoice(); // Отправляем голос сразу после подключения
      break;
    case 'voice_changed':
      serverVoice = msg.voice;
      report('running', `Голос: ${msg.voice}`);
      break;
    case 'no_speech':
    case 'info':
      break;
    default:
      console.debug('[server]', msg);
  }
}

// ------------------------------------------------------------ воспроизведение
// --- НОВАЯ ФУНКЦИЯ ДЛЯ ПРИГЛУШЕНИЯ ---
function duckOriginal(isDucked) {
  if (!originalGain || !ctx) return;
  // Приглушаем до 15% ОТ ТЕКУЩЕЙ громкости ползунка, а не от 100%
  const targetVolume = isDucked ? (currentOrigVol * 0.15) : currentOrigVol; 
  originalGain.gain.setTargetAtTime(targetVolume, ctx.currentTime, 0.1);
}

async function playMp3(b64, seq) {
  if (!ctx || !active) return;
  try {
    const audioBuffer = await ctx.decodeAudioData(base64ToBytes(b64).buffer);
    const src = ctx.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(translationGain);

    // Включаем приглушение
    duckOriginal(true);

    // Возвращаем звук после окончания
    src.onended = () => {
      if (nextPlayTime - ctx.currentTime < 0.1) {
        duckOriginal(false);
      }
    };

    const startAt = Math.max(ctx.currentTime + 0.05, nextPlayTime);
    src.start(startAt);
    nextPlayTime = startAt + audioBuffer.duration;
  } catch (err) {
    console.error(`[offscreen] не удалось воспроизвести seq=${seq}`, err);
    duckOriginal(false);
  }
}

// ------------------------------------------------------------ base64
function bytesToBase64(bytes) {
  let bin = '';
  const STEP = 0x8000; // по кускам, чтобы не упереться в лимит аргументов apply()
  for (let i = 0; i < bytes.length; i += STEP) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + STEP));
  }
  return btoa(bin);
}

function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}