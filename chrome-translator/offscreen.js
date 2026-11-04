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

// --- НОВЫЕ КОНСТАНТЫ ДЛЯ ПЛАВНОГО ВОСПРОИЗВЕДЕНИЯ ---
const LEAD = 0.08;            // Запас перед стартом первого куска (сек)
const PAUSE = 0.06;           // Естественная пауза между кусками
const FADE = 0.015;           // Плавное появление/затухание (против щелчков)
const SILENCE_THR = 0.01;     // Порог для обрезки тишины по краям
const CATCHUP_FROM = 1.5;     // С какого отставания (сек) начинаем ускорять
const MAX_RATE = 1.25;        // Максимальное ускорение (1.25 = на 25% быстрее)
const MAX_BACKLOG = 8.0;      // Если отстали на 8 сек — сбрасываем очередь
const DUCK_LEVEL = 0.15;      // Приглушение оригинала (15% от текущей громкости)
const DUCK_RELEASE_MS = 350;  // Задержка перед возвратом звука (чтобы не «качало»)

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
        
        applyOriginalGain(); // Применяем громкость с учетом ducking
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
    // --- НОВОЕ ---
  clearTimeout(duckTimer);
  scheduled.clear();
  ducked = false;
  // ------------
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
  if (!ws || ws.readyState !== WebSocket.OPEN) return; 
  if (ws.bufferedAmount > MAX_BUFFERED) return;         
  ws.send(buffer); // Отправляем сырые байты, сервер уже умеет их принимать!
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
let scheduled = new Set();
let ducked = false;
let duckTimer = null;

function applyOriginalGain() {
  if (!originalGain || !ctx) return;
  const target = currentOrigVol * (ducked ? DUCK_LEVEL : 1);
  originalGain.gain.setTargetAtTime(target, ctx.currentTime, 0.1);
}

function setDuck(on) {
  clearTimeout(duckTimer);
  if (on) {
    ducked = true;
    applyOriginalGain();
  } else {
    duckTimer = setTimeout(() => {
      ducked = false;
      applyOriginalGain();
    }, DUCK_RELEASE_MS);
  }
}

function trimSilence(buf) {
  const d = buf.getChannelData(0);
  const sampleRate = buf.sampleRate;
  const SILENCE_THR = 0.01; // Порог тишины
  const MIN_PAUSE = 0.3;    // Вырезаем паузы дольше 300 мс
  
  const minPauseSamples = MIN_PAUSE * sampleRate;
  const pad = Math.round(0.02 * sampleRate);

  // 1. Находим границы речи (начало и конец)
  let a = 0, b = d.length - 1;
  while (a < b && Math.abs(d[a]) < SILENCE_THR) a++;
  while (b > a && Math.abs(d[b]) < SILENCE_THR) b--;
  a = Math.max(0, a - pad);
  b = Math.min(d.length - 1, b + pad);

  // 2. Ищем длинные паузы внутри и склеиваем речь
  const newLength = b - a + 1;
  const out = new Float32Array(newLength);
  let outIdx = 0;
  let silenceCount = 0;
  let inSpeech = false;

  for (let i = a; i <= b; i++) {
    const isSilent = Math.abs(d[i]) < SILENCE_THR;

    if (isSilent) {
      silenceCount++;
      // Если пауза превысила лимит, не копируем её (вырезаем)
      if (silenceCount > minPauseSamples) {
        inSpeech = false;
        continue; 
      }
    } else {
      silenceCount = 0;
      inSpeech = true;
    }
    
    out[outIdx++] = d[i];
  }

  // Создаем новый буфер
  const result = ctx.createBuffer(1, outIdx, sampleRate);
  result.getChannelData(0).set(out.subarray(0, outIdx));

  return { buffer: result, offset: 0, duration: result.duration };
}

function dropScheduled() {
  for (const s of scheduled) { try { s.stop(); } catch {} }
  scheduled.clear();
  nextPlayTime = 0;
}

async function playMp3(b64, seq) {
  if (!ctx || !active) return;
  let audioBuffer;
  try {
    audioBuffer = await ctx.decodeAudioData(base64ToBytes(b64).buffer);
  } catch (err) {
    console.error(`[offscreen] decode seq=${seq}`, err);
    return;
  }
  if (!ctx || !active) return; 

  const now = ctx.currentTime;
  let backlog = Math.max(0, nextPlayTime - now);

  // Если отстали слишком сильно, сбрасываем накопленное
  if (backlog > MAX_BACKLOG) {
    dropScheduled();
    backlog = 0;
  }

  // Плавное ускорение при отставании
  const rate = 1.0; // Всегда играем с нормальной скоростью, никакого ускорения
  const { buffer: trimmedBuffer, offset, duration } = trimSilence(audioBuffer);
  const realDur = duration / rate;

  const src = ctx.createBufferSource();
  src.buffer = trimmedBuffer; // используем обрезанный буфер
  src.playbackRate.value = rate; 

  const env = ctx.createGain(); // Огибающая для фейдов
  src.connect(env).connect(translationGain);

  const startAt = backlog > 0 ? nextPlayTime + PAUSE : now + LEAD;

  // Настраиваем плавное появление и затухание
  env.gain.setValueAtTime(0, startAt);
  env.gain.linearRampToValueAtTime(1, startAt + FADE);
  env.gain.setValueAtTime(1, Math.max(startAt + FADE, startAt + realDur - FADE));
  env.gain.linearRampToValueAtTime(0, startAt + realDur);

  scheduled.add(src);
  setDuck(true); // Приглушаем оригинал
  
  src.onended = () => {
    scheduled.delete(src);
    if (scheduled.size === 0) setDuck(false); // Возвращаем звук, только когда всё доиграло
  };

  src.start(startAt, offset, duration);
  nextPlayTime = startAt + realDur;
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