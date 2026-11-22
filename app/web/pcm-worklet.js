// Runs in an AudioContext created at 16 kHz: converts float samples to 16-bit PCM
// and posts 100 ms chunks plus an RMS level for the input meter.
class PCM16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = 1600;
    this.buf = new Int16Array(this.size);
    this.n = 0;
    this.sum = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        let v = ch[i];
        if (v > 1) v = 1; else if (v < -1) v = -1;
        this.sum += v * v;
        this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.n === this.size) {
          const level = Math.sqrt(this.sum / this.size);
          this.port.postMessage({ pcm: this.buf.buffer, level }, [this.buf.buffer]);
          this.buf = new Int16Array(this.size);
          this.n = 0;
          this.sum = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm16", PCM16);
