// Live particle field behind the page (replaces the looping hero video).
//
// Lit 3D spheres, silver with warm orange accents, form a "voice orb" that morphs into a
// different shape for each section: two orbs talking, a waveform, Indian scripts, a ring.
// Warm dust drifts behind them. Particles move away from the cursor or a finger, a click
// sends a shockwave, and the orb pulses with voice energy. Plain Canvas 2D, no libraries.

const TAU = Math.PI * 2;
const CAMERA = 3.4; // distance from the camera to the centre of the scene

// Each layout: [centre x, centre y (fractions of the viewport), size, brightness]
const LAYOUTS = {
  wide: {
    orb: [0.5, 0.35, 0.86, 1],
    duo: [0.5, 0.5, 1, 0.65],
    wave: [0.5, 0.5, 0.95, 0.85],
    glyph: [0.5, 0.48, 1.05, 0.8],
    ring: [0.5, 0.5, 1.05, 0.6],
    cloud: [0.5, 0.5, 1.1, 0.55],
    cta: [0.5, 0.45, 0.9, 0.85],
    setup: [0.5, 0.5, 1.15, 0.18],
    room: [0.5, 0.15, 0.42, 0.3],
  },
  narrow: {
    orb: [0.5, 0.29, 0.92, 1],
    duo: [0.5, 0.5, 0.85, 0.55],
    wave: [0.5, 0.5, 0.75, 0.75],
    glyph: [0.5, 0.45, 0.95, 0.7],
    ring: [0.5, 0.5, 0.85, 0.5],
    cloud: [0.5, 0.5, 1, 0.5],
    cta: [0.5, 0.36, 0.8, 0.8],
    setup: [0.5, 0.45, 1, 0.15],
    room: [0.5, 0.12, 0.38, 0.28],
  },
};
const SHAPE_OF = { cta: "orb", setup: "orb", room: "orb" };
// Continuous spin for round shapes. The rest sway gently and face the viewer, so the two
// people stay side by side and the waveform and letters stay readable.
const SPIN = { orb: 0.16, ring: 0.1, cloud: 0.05 };
const SWAY = 0.12; // radians
const TILT = { round: 0.16, flat: 0.05 };

const GLYPHS = ["अ", "த", "ಕ", "క", "മ", "ক", "ગ", "ਪ"];
const GLYPH_SECONDS = 2.6;
const GLYPH_FONT = '"Nirmala UI", "Noto Sans Devanagari", "Noto Sans Tamil", "Noto Sans Kannada", "Noto Sans Telugu", "Noto Sans Malayalam", "Noto Sans Bengali", "Noto Sans Gujarati", "Noto Sans Gurmukhi", "Noto Sans", sans-serif';

// ---------- sprites, drawn once ----------

function sphereSprite(stops, size = 64) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const g = canvas.getContext("2d");
  // Light comes from the top left, so each dot reads as a small lit ball
  const grad = g.createRadialGradient(size * 0.36, size * 0.32, size * 0.03, size * 0.5, size * 0.5, size * 0.5);
  for (const [offset, color] of stops) grad.addColorStop(offset, color);
  g.fillStyle = grad;
  g.beginPath();
  g.arc(size / 2, size / 2, size / 2 - 0.5, 0, TAU);
  g.fill();
  return canvas;
}

function glowSprite(rgb, size = 32) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const g = canvas.getContext("2d");
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, `rgba(${rgb},1)`);
  grad.addColorStop(0.25, `rgba(${rgb},0.55)`);
  grad.addColorStop(1, `rgba(${rgb},0)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return canvas;
}

// ---------- glyph shapes: sample points from text drawn off-screen ----------

function sampleGlyph(char, count) {
  const size = 240;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const g = canvas.getContext("2d", { willReadFrequently: true });
  g.fillStyle = "#fff";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.font = `700 ${size * 0.72}px ${GLYPH_FONT}`;
  g.fillText(char, size / 2, size / 2);
  const data = g.getImageData(0, 0, size, size).data;
  const xs = [];
  const ys = [];
  for (let y = 0; y < size; y += 2) {
    for (let x = 0; x < size; x += 2) {
      if (data[(y * size + x) * 4 + 3] > 140) {
        xs.push(x);
        ys.push(y);
      }
    }
  }
  if (xs.length < 60) return null; // no font for this script on this device
  // Centre the glyph on its own bounding box and scale it to about [-1, 1]
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(maxX - minX, maxY - minY) / 2;
  const points = new Float32Array(count * 2);
  for (let i = 0; i < count; i++) {
    const j = Math.floor(Math.random() * xs.length);
    points[i * 2] = (xs[j] - cx + Math.random() * 2 - 1) / half;
    points[i * 2 + 1] = (ys[j] - cy + Math.random() * 2 - 1) / half;
  }
  return points;
}

// ---------- the field ----------

export function createParticleField(canvas) {
  const ctx = canvas.getContext("2d");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const narrowQuery = matchMedia("(max-width: 900px)");

  const sprites = [
    sphereSprite([[0, "#ffffff"], [0.22, "#eef0f5"], [0.62, "#9da3ae"], [0.9, "#4a4e57"], [1, "#24262b"]]),
    sphereSprite([[0, "#fff1e6"], [0.25, "#ffb27a"], [0.65, "#f26b1d"], [0.92, "#7a2c08"], [1, "#2e1003"]]),
  ];
  const dustSprites = [glowSprite("255,244,232"), glowSprite("255,150,80")];

  // Fewer particles on small screens keeps phones smooth
  const area = innerWidth * innerHeight;
  const N = area > 1.2e6 ? 2400 : area > 6e5 ? 1800 : 1100;
  const DUST = Math.round(N * 0.22);
  let drawN = N;

  // Per-particle data (structure of arrays: fast and garbage-free)
  const u1 = new Float32Array(N), u2 = new Float32Array(N), u3 = new Float32Array(N);
  const dx = new Float32Array(N), dy = new Float32Array(N), dz = new Float32Array(N);
  const theta = new Float32Array(N), phi = new Float32Array(N), inner = new Float32Array(N);
  const base = new Float32Array(N), rate = new Float32Array(N), kind = new Uint8Array(N);
  const px = new Float32Array(N), py = new Float32Array(N), pz = new Float32Array(N);
  const ox = new Float32Array(N), oy = new Float32Array(N), vx = new Float32Array(N), vy = new Float32Array(N);
  const sx = new Float32Array(N), sy = new Float32Array(N), ss = new Float32Array(N), sa = new Float32Array(N);
  const depth = new Float32Array(N);
  const order = new Uint16Array(N);

  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    u1[i] = Math.random();
    u2[i] = Math.random();
    u3[i] = Math.random();
    // Even directions over a sphere (Fibonacci lattice), in shuffled order
    const k = (i * 7919) % N;
    const y = 1 - (k / (N - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    dx[i] = Math.cos(golden * k) * r;
    dy[i] = y;
    dz[i] = Math.sin(golden * k) * r;
    theta[i] = Math.atan2(dz[i], dx[i]);
    phi[i] = Math.acos(dy[i]);
    inner[i] = u3[i] < 0.72 ? 1 : 0.35 + 0.6 * Math.cbrt(u2[i]); // most on the surface, some inside for depth
    // Many small, a few large. Size gets its own random number: reusing a shape seed would
    // put all the big particles at one end of the waveform or ring.
    base[i] = 1.3 + Math.pow(Math.random(), 3.2) * 6.5;
    rate[i] = 0.03 + u2[i] * 0.055; // different speeds make morphs look organic
    kind[i] = u3[i] > 0.86 ? 1 : 0; // ~14% orange
    px[i] = dx[i] * 3;
    py[i] = dy[i] * 3;
    pz[i] = dz[i] * 3;
    order[i] = i;
  }

  const dX = new Float32Array(DUST), dY = new Float32Array(DUST), dZ = new Float32Array(DUST);
  const dV = new Float32Array(DUST), dS = new Float32Array(DUST), dK = new Uint8Array(DUST);
  for (let i = 0; i < DUST; i++) {
    dX[i] = (Math.random() * 2 - 1) * 3.4;
    dY[i] = (Math.random() * 2 - 1) * 2.2;
    dZ[i] = (Math.random() * 2 - 1) * 2;
    dV[i] = 0.015 + Math.random() * 0.04;
    dS[i] = 1 + Math.random() * 3.2;
    dK[i] = Math.random() < 0.45 ? 1 : 0;
  }

  const glyphCache = new Map();
  let glyphIndex = 0;
  let glyphPoints = null;
  let glyphTimer = 0;

  let width = 0, height = 0, dpr = 1;
  let formation = "orb";
  let shape = "orb";
  const layout = [...LAYOUTS.wide.orb];
  let fade = 0; // fades the whole field in on load
  let rotY = 0, rotX = 0.18;
  let energy = 0, energyHold = 0, energyPulse = 0;
  const mouse = { x: -9999, y: -9999, tx: 0, ty: 0, nx: 0, ny: 0, active: false };
  const ripples = [];
  let running = false, last = 0, time = 0, slowFrames = 0, frameAvg = 6;
  let releaseTimer = null;

  function resize() {
    dpr = Math.min(devicePixelRatio || 1, 1.6);
    width = innerWidth;
    height = innerHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    if (reducedMotion) draw(0);
  }

  function targetLayout() {
    return (narrowQuery.matches ? LAYOUTS.narrow : LAYOUTS.wide)[formation];
  }

  function glyph(index) {
    for (let tries = 0; tries < GLYPHS.length; tries++) {
      const char = GLYPHS[(index + tries) % GLYPHS.length];
      if (!glyphCache.has(char)) glyphCache.set(char, sampleGlyph(char, N));
      const points = glyphCache.get(char);
      if (points) return points;
    }
    return null;
  }

  // ---------- simulation ----------

  function step(dt) {
    time += dt;
    const t = time;
    const f60 = Math.min(dt * 60, 3);

    fade = Math.min(1, fade + dt * 0.7);
    const goal = targetLayout();
    for (let k = 0; k < 4; k++) layout[k] += (goal[k] - layout[k]) * 0.05 * f60;

    energyPulse *= Math.pow(0.9, f60);
    const energyGoal = Math.min(1, Math.max(energyHold, energyPulse));
    energy += (energyGoal - energy) * 0.2 * f60;

    // Cursor tilt, smoothed
    mouse.nx += (mouse.tx - mouse.nx) * 0.05 * f60;
    mouse.ny += (mouse.ty - mouse.ny) * 0.05 * f60;

    const round = SPIN[shape] !== undefined;
    if (round) {
      rotY += SPIN[shape] * dt * (1 + energy * 2);
    } else {
      rotY = Math.atan2(Math.sin(rotY), Math.cos(rotY)); // keep within ±π before easing
      rotY += (SWAY * Math.sin(t * 0.5) - rotY) * 0.04 * f60;
    }
    rotX += ((round ? TILT.round : TILT.flat) + mouse.ny * 0.3 - rotX) * 0.05 * f60;

    if (shape === "glyph") {
      glyphTimer += dt;
      if (!glyphPoints || glyphTimer > GLYPH_SECONDS) {
        if (glyphPoints) glyphIndex = (glyphIndex + 1) % GLYPHS.length;
        glyphTimer = 0;
        glyphPoints = glyph(glyphIndex);
        if (!glyphPoints) shape = "orb";
      }
    }

    for (let i = 0; i < drawN; i++) {
      let x, y, z;
      switch (shape) {
        case "duo": {
          // Two people: two small orbs, and a stream of words flowing between them
          const side = i & 1 ? 1 : -1;
          if (u3[i] < 0.2) {
            const s = (u1[i] + t * 0.11 * side + 10) % 1;
            x = -0.82 + 1.64 * s;
            y = -0.42 * Math.sin(s * Math.PI) + (u2[i] - 0.5) * 0.09;
            z = (u2[i] - 0.5) * 0.12 + Math.sin(s * TAU * 2 + t) * 0.05;
          } else {
            const r = 0.5 * inner[i] * (1 + 0.06 * Math.sin(3 * theta[i] + t * 1.4 + side) + energy * 0.2);
            x = side * 0.82 + dx[i] * r;
            y = dy[i] * r;
            z = dz[i] * r;
          }
          break;
        }
        case "wave": {
          // A live audio waveform: vertical bars of particles
          const bars = 56;
          const b = Math.floor(u1[i] * bars);
          const amp = 0.1 + 0.6 * Math.abs(Math.sin(b * 0.42 + t * 2.1) * Math.sin(b * 0.11 - t * 1.25 + 1)) + energy * 0.5;
          x = (b / (bars - 1) * 2 - 1) * 1.75 + (u3[i] - 0.5) * 0.02;
          y = (u2[i] * 2 - 1) * amp;
          z = (u3[i] - 0.5) * 0.24;
          break;
        }
        case "glyph": {
          x = glyphPoints[i * 2] * 1.1;
          y = glyphPoints[i * 2 + 1] * 1.1;
          z = (u3[i] - 0.5) * 0.22 + 0.04 * Math.sin(t * 2 + u1[i] * TAU);
          break;
        }
        case "ring": {
          const a = u1[i] * TAU + t * 0.3;
          const b = u2[i] * TAU;
          const R = 1 + energy * 0.08;
          const rr = 0.2 * (0.55 + 0.45 * inner[i]);
          const rx = (R + rr * Math.cos(b)) * Math.cos(a);
          const ry = rr * Math.sin(b);
          const rz = (R + rr * Math.cos(b)) * Math.sin(a);
          const tilt = 1.12;
          x = rx;
          y = ry * Math.cos(tilt) - rz * Math.sin(tilt);
          z = ry * Math.sin(tilt) + rz * Math.cos(tilt);
          break;
        }
        case "cloud": {
          const r = 0.25 + 1.4 * Math.pow(u1[i], 0.6);
          const a = t * 0.07 * (0.5 + u2[i]);
          const cx0 = dx[i] * r, cz0 = dz[i] * r;
          x = cx0 * Math.cos(a) - cz0 * Math.sin(a);
          y = dy[i] * r * 0.8;
          z = cx0 * Math.sin(a) + cz0 * Math.cos(a);
          break;
        }
        default: {
          // The voice orb: breathes slowly, ripples when someone speaks
          const wobble = 1
            + 0.07 * Math.sin(3 * theta[i] + t * 1.1) * Math.cos(2 * phi[i] - t * 0.8)
            + energy * 0.3 * Math.sin(theta[i] * 6 + t * 8 + u1[i] * 6);
          const r = 0.95 * inner[i] * wobble;
          x = dx[i] * r;
          y = dy[i] * r;
          z = dz[i] * r;
        }
      }
      const k = rate[i] * f60;
      px[i] += (x - px[i]) * k;
      py[i] += (y - py[i]) * k;
      pz[i] += (z - pz[i]) * k;
    }

    project(f60);
  }

  function project(f60) {
    const cy = Math.cos(rotY + mouse.nx * 0.45), sy0 = Math.sin(rotY + mouse.nx * 0.45);
    const cx = Math.cos(rotX), sx0 = Math.sin(rotX);
    const S = Math.min(width, height * 1.25) * 0.27 * layout[2];
    const centerX = width * layout[0], centerY = height * layout[1];
    const sizeScale = S / 260;
    const R = narrowQuery.matches ? 110 : 150;
    const R2 = R * R;
    const now = performance.now();
    for (let r = ripples.length - 1; r >= 0; r--) if (now - ripples[r].t0 > 1300) ripples.splice(r, 1);

    for (let i = 0; i < drawN; i++) {
      // Rotate around Y, then X
      const x1 = px[i] * cy + pz[i] * sy0;
      const z1 = -px[i] * sy0 + pz[i] * cy;
      const y1 = py[i] * cx - z1 * sx0;
      const z2 = py[i] * sx0 + z1 * cx;
      const persp = CAMERA / (CAMERA + z2);
      const bx = centerX + x1 * S * persp;
      const by = centerY + y1 * S * persp;

      // Cursor and shockwaves push particles in screen space; a spring brings them back
      let glow = 0;
      let fx = 0, fy = 0;
      const ex = bx + ox[i] - mouse.x, ey = by + oy[i] - mouse.y;
      const d2 = ex * ex + ey * ey;
      if (mouse.active && d2 < R2 && d2 > 0.01) {
        const d = Math.sqrt(d2);
        const f = 1 - d / R;
        fx += (ex / d) * f * f * 3.4;
        fy += (ey / d) * f * f * 3.4;
        glow = f;
      }
      for (const ripple of ripples) {
        const rx = bx - ripple.x, ry = by - ripple.y;
        const d = Math.sqrt(rx * rx + ry * ry) || 1;
        const age = (now - ripple.t0) / 1000;
        const gap = Math.abs(d - age * 820);
        if (gap < 60) {
          const f = (1 - gap / 60) * 7 * (1 - age / 1.3);
          fx += (rx / d) * f;
          fy += (ry / d) * f;
          glow = Math.max(glow, (1 - gap / 60) * 0.8);
        }
      }
      vx[i] = (vx[i] + (fx - ox[i] * 0.045) * f60) * Math.pow(0.86, f60);
      vy[i] = (vy[i] + (fy - oy[i] * 0.045) * f60) * Math.pow(0.86, f60);
      ox[i] += vx[i] * f60;
      oy[i] += vy[i] * f60;

      sx[i] = bx + ox[i];
      sy[i] = by + oy[i];
      ss[i] = base[i] * persp * sizeScale * (1 + glow * 0.7);
      // Depth fog: far particles dim towards black
      const fog = Math.min(1, Math.max(0.16, 1 - (z2 + 1.2) / 3.2 * 0.85));
      sa[i] = Math.min(1, fog + glow * 0.5);
      depth[i] = z2;
    }
  }

  // ---------- drawing ----------

  function draw(dt) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const alpha = layout[3] * fade;
    if (alpha < 0.01) return;

    // Dust: soft glowing specks drifting slowly, with a little cursor parallax
    ctx.globalCompositeOperation = "lighter";
    const S = Math.min(width, height * 1.25) * 0.27;
    for (let i = 0; i < DUST; i++) {
      dY[i] -= dV[i] * dt;
      if (dY[i] < -2.3) dY[i] = 2.3;
      const persp = CAMERA / (CAMERA + dZ[i]);
      const x = width / 2 + (dX[i] + mouse.nx * 0.25 * (dZ[i] + 2)) * S * persp;
      const y = height / 2 + (dY[i] + mouse.ny * 0.15 * (dZ[i] + 2)) * S * persp;
      const s = dS[i] * persp * 2.2;
      const twinkle = 0.55 + 0.45 * Math.sin(time * (0.8 + dV[i] * 20) + i);
      ctx.globalAlpha = alpha * 0.32 * twinkle * persp;
      ctx.drawImage(dustSprites[dK[i]], x - s, y - s, s * 2, s * 2);
    }

    // Spheres, far to near so nearer ones overlap farther ones
    ctx.globalCompositeOperation = "source-over";
    const sorted = order.subarray(0, drawN).sort((a, b) => depth[b] - depth[a]);
    for (let n = 0; n < sorted.length; n++) {
      const i = sorted[n];
      const s = ss[i];
      if (s < 0.5) continue;
      const x = sx[i], y = sy[i];
      if (x < -s || y < -s || x > width + s || y > height + s) continue;
      ctx.globalAlpha = sa[i] * alpha;
      ctx.drawImage(sprites[kind[i]], x - s / 2, y - s / 2, s, s);
    }
    ctx.globalAlpha = 1;
  }

  function frame(now) {
    if (!running) return;
    const dt = Math.min((now - (last || now)) / 1000, 0.05);
    last = now;
    const workStart = performance.now();
    step(dt);
    draw(dt);
    // If the device can't keep up, draw fewer particles rather than stutter. This measures our
    // own work per frame, not the time between frames, which a throttled background tab stretches.
    frameAvg = frameAvg * 0.95 + (performance.now() - workStart) * 0.05;
    if (frameAvg > 12 && drawN > 500) {
      if (++slowFrames > 90) {
        drawN = Math.floor(drawN * 0.82);
        for (let i = 0; i < drawN; i++) order[i] = i; // only sort particles still in use
        slowFrames = 0;
      }
    } else {
      slowFrames = 0;
    }
    requestAnimationFrame(frame);
  }

  function start() {
    if (running || reducedMotion) return;
    running = true;
    last = 0;
    requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
  }

  // ---------- input ----------

  function onMove(e) {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
    mouse.tx = (e.clientX / width) * 2 - 1;
    mouse.ty = (e.clientY / height) * 2 - 1;
    mouse.active = true;
    clearTimeout(releaseTimer);
    if (e.pointerType === "touch") releaseTimer = setTimeout(() => { mouse.active = false; }, 700);
  }

  addEventListener("pointermove", onMove, { passive: true });
  addEventListener("pointerdown", (e) => {
    onMove(e);
    if (!e.target.closest?.("input, select, textarea, .talk-button")) {
      ripples.push({ x: e.clientX, y: e.clientY, t0: performance.now() });
      if (ripples.length > 4) ripples.shift();
    }
  }, { passive: true });
  document.addEventListener("pointerleave", () => { mouse.active = false; mouse.tx = mouse.ty = 0; });
  addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));

  resize();
  if (reducedMotion) {
    // One still frame, already formed
    for (let i = 0; i < N; i++) {
      const r = 0.95 * inner[i];
      px[i] = dx[i] * r; py[i] = dy[i] * r; pz[i] = dz[i] * r;
    }
    fade = 1;
    layout.splice(0, 4, ...targetLayout());
    project(1);
    draw(0);
  } else {
    start();
  }

  return {
    // Which shape to form: orb, duo, wave, glyph, ring, cloud, cta, setup, room
    setFormation(name) {
      if (!LAYOUTS.wide[name] || name === formation) return;
      formation = name;
      shape = SHAPE_OF[name] || name;
      if (shape === "glyph") glyphTimer = GLYPH_SECONDS; // switch letters right away
      if (reducedMotion) {
        layout.splice(0, 4, ...targetLayout());
        project(1);
        draw(0);
      }
    },
    // Held level (e.g. microphone) from 0 to 1
    setEnergy(value) {
      energyHold = Math.max(0, Math.min(1, value));
    },
    // A short burst (e.g. a chunk of audio playing)
    pulse(value) {
      energyPulse = Math.max(energyPulse, Math.min(1, value));
    },
  };
}
