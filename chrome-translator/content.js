let subtitleDiv = null;
let hideTimeout = null;
let currentSettings = { show: true, size: '24' };
let playerObserver = null;

// Загружаем настройки при старте
chrome.storage.local.get(['showSubs', 'subSize'], (res) => {
  currentSettings.show = res.showSubs ?? true;
  currentSettings.size = res.subSize ?? '24';
  if (subtitleDiv) updateStyle();
});

function updateStyle() {
  if (!subtitleDiv) return;
  subtitleDiv.style.setProperty('font-size', `${currentSettings.size}px`, 'important');
  subtitleDiv.style.setProperty('display', currentSettings.show ? 'block' : 'none', 'important');
}

function waitForPlayerAndInit() {
  if (subtitleDiv) return;

  const player = document.querySelector('.html5-video-player') 
              || document.querySelector('video')?.parentElement;

  if (player) {
    initSubtitles();
    return;
  }

  if (!playerObserver) {
    playerObserver = new MutationObserver(() => {
      const p = document.querySelector('.html5-video-player') 
             || document.querySelector('video')?.parentElement;
      if (p) {
        playerObserver.disconnect();
        playerObserver = null;
        initSubtitles();
      }
    });
    playerObserver.observe(document.body, { childList: true, subtree: true });
  }
}

function initSubtitles() {
  if (subtitleDiv) return;
  
  subtitleDiv = document.createElement('div');
  subtitleDiv.style.setProperty('position', 'absolute', 'important');
  subtitleDiv.style.setProperty('bottom', '15%', 'important'); 
  subtitleDiv.style.setProperty('left', '50%', 'important');
  subtitleDiv.style.setProperty('transform', 'translateX(-50%)', 'important');
  subtitleDiv.style.setProperty('background-color', 'rgba(0, 0, 0, 0.8)', 'important');
  subtitleDiv.style.setProperty('color', 'white', 'important');
  subtitleDiv.style.setProperty('padding', '8px 16px', 'important');
  subtitleDiv.style.setProperty('border-radius', '8px', 'important');
  subtitleDiv.style.setProperty('font-family', 'Arial, sans-serif', 'important');
  subtitleDiv.style.setProperty('font-weight', 'bold', 'important');
  subtitleDiv.style.setProperty('text-align', 'center', 'important');
  subtitleDiv.style.setProperty('text-shadow', '2px 2px 4px rgba(0,0,0,0.8)', 'important');
  subtitleDiv.style.setProperty('max-width', '90%', 'important');
  subtitleDiv.style.setProperty('word-wrap', 'break-word', 'important');
  subtitleDiv.style.setProperty('z-index', '1000', 'important');
  subtitleDiv.style.setProperty('pointer-events', 'none', 'important'); 
  subtitleDiv.style.setProperty('transition', 'opacity 0.3s ease', 'important');
  subtitleDiv.style.setProperty('opacity', '0', 'important');

  const player = document.querySelector('.html5-video-player') 
              || document.querySelector('video')?.parentElement 
              || document.body;
  
  player.appendChild(subtitleDiv);
  updateStyle();
  
  if (player !== document.body) {
      const observer = new MutationObserver(() => {
          if (subtitleDiv && !player.contains(subtitleDiv)) {
              player.appendChild(subtitleDiv);
          }
      });
      observer.observe(player, { childList: true, subtree: false });
  }
}

// Запускаем ожидание плеера сразу при загрузке content.js
waitForPlayerAndInit();

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== 'content') return;
  
  // Обработка смены настроек из popup
  if (msg.type === 'update-sub-settings') {
    currentSettings.show = msg.show;
    currentSettings.size = msg.size;
    updateStyle();
  }

  if (msg.type === 'subtitle') {
    if (!subtitleDiv) {
      // Плеер ещё не готов — ждём и показываем текст с задержкой
      waitForPlayerAndInit();
      setTimeout(() => {
        if (!subtitleDiv) return;
        subtitleDiv.textContent = msg.text;
        if (currentSettings.show) {
          subtitleDiv.style.setProperty('opacity', '1', 'important');
        }
        clearTimeout(hideTimeout);
        hideTimeout = setTimeout(() => {
          subtitleDiv.style.setProperty('opacity', '0', 'important');
        }, 4000);
      }, 300);
      return;
    }
    
    subtitleDiv.textContent = msg.text;
    
    if (currentSettings.show) {
      subtitleDiv.style.setProperty('opacity', '1', 'important'); 
    }
    
    clearTimeout(hideTimeout);
    hideTimeout = setTimeout(() => {
      subtitleDiv.style.setProperty('opacity', '0', 'important'); 
    }, 4000);
  }
});