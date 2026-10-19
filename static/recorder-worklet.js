// Captures raw microphone samples while recording and posts them to the page in blocks.
// 'stop' flushes what is left, then posts 'done' so the page knows it has everything.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.recording = false;
    this.buffer = new Float32Array(4096);
    this.length = 0;
    this.port.onmessage = (event) => {
      if (event.data === "start") {
        this.length = 0;
        this.recording = true;
      } else if (event.data === "stop") {
        this.recording = false;
        this.flush();
        this.port.postMessage("done");
      }
    };
  }

  flush() {
    if (this.length) {
      this.port.postMessage(this.buffer.slice(0, this.length));
      this.length = 0;
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (this.recording && channel) {
      for (let i = 0; i < channel.length; i++) {
        this.buffer[this.length++] = channel[i];
        if (this.length === this.buffer.length) this.flush();
      }
    }
    return true;
  }
}

registerProcessor("capture", Capture);
