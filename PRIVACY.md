# SyncLingua Privacy Policy

**Last updated: October 27, 2026**

SyncLingua is a Chrome extension designed for real-time audio translation. We take your privacy extremely seriously.

## 1. Data Collection and Usage
SyncLingua **does not collect, store, or transmit any personal data** to external servers. 

When you use the extension, the audio from the active tab is captured and sent **only to your local computer** (specifically, to the local server running at `http://127.0.0.1:8000`). 

## 2. Local Processing
All audio processing, speech recognition (Whisper), translation (Ollama), and voice synthesis (Piper) are performed **locally on your machine**. No audio data, text, or personal information ever leaves your computer.

## 3. Permissions
- `tabCapture`: Required to capture the audio of the active tab for translation.
- `offscreen`: Required to process audio in the background and play the translated audio.
- `storage`: Required to save your settings (volume, voice choice, subtitle size) locally in your browser.

## 4. Contact
If you have any questions about this privacy policy, please open an issue on our GitHub repository.
