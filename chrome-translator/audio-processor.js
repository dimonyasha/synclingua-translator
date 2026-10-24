// audio-processor.js
class PCM16Processor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const { targetRate = 16000, chunkMs = 160 } = options.processorOptions ?? {};
    this.ratio = sampleRate / targetRate;
    this.chunkSamples = Math.round((targetRate * chunkMs) / 1000);
    this.out = new Int16Array(this.chunkSamples);
    this.outIdx = 0;
    this.acc = 0;
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      const s = channel[i];
      let remaining = 1;

      while (remaining > 1e-9) {
        const take = Math.min(remaining, this.ratio - this.filled);
        this.acc += s * take;
        this.filled += take;
        remaining -= take;

        if (this.filled >= this.ratio - 1e-9) {
          this.pushSample(this.acc / this.ratio);
          this.acc = 0;
          this.filled = 0;
        }
      }
    }
    return true;
  }

  pushSample(v) {
    const c = Math.max(-1, Math.min(1, v));
    this.out[this.outIdx++] = c < 0 ? c * 0x8000 : c * 0x7fff;

    if (this.outIdx === this.chunkSamples) {
      this.port.postMessage(this.out.buffer, [this.out.buffer]);
      this.out = new Int16Array(this.chunkSamples);
      this.outIdx = 0;
    }
  }
}

registerProcessor('pcm16-processor', PCM16Processor);