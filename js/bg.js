(function() {
  'use strict';

  // ============================================================
  //  弹性网格 — 跟随滚动，鼠标径向推动
  //  底部 overscroll 产生涟漪向上扩散
  // ============================================================

  const canvas = document.createElement('canvas');
  canvas.id = 'bg-canvas';
  canvas.style.cssText =
    'position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;' +
    'pointer-events:none;display:block';
  document.body.prepend(canvas);

  const isMobile = window.innerWidth < 768;
  const ctx = canvas.getContext('2d');
  const DPR = Math.min(window.devicePixelRatio || 1, isMobile ? 1 : 2);
  let animId = null;

  const SPACING = 26;
  const SPRING_K = 0.035;
  const DAMPING = 0.95;
  const COUPLING = 0.12;
  const MOUSE_RADIUS = isMobile ? 60 : 140;

  // ---- grid ----
  let W, H;
  let cols, rows;
  let grid = [];

  // ---- mouse ----
  let mX = -9999, mY = -9999;
  let effX = -9999, effY = -9999;
  let prevX = -9999, prevY = -9999;
  let speed = 0;
  let scrollY = 0;
  let prevScrollY = 0;

  // ---- overscroll ----
  let bottomTap = 0;          // 底部短促脉冲
  let bottomPull = 0;         // 底部持续拉伸
  let topTap = 0;             // 顶部短促脉冲
  let topPull = 0;            // 顶部持续拉伸
  let lastWheelTime = 0;

  // ---- click ripples ----
  let ripples = [];

  // ---- easter egg chaos ----
  let chaosActive = false;
  let chaosEnergy = 0;
  let chaosPhase = 0;

  // ---- helpers ----
  function getAccent() {
    const el = document.documentElement;
    const val = getComputedStyle(el).getPropertyValue('--theme-color').trim() || '#399FFF';
    const c = val.replace('#', '');
    const v = parseInt(c.length === 3 ? c.split('').map(x => x + x).join('') : c, 16);
    return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }

  function isDark() {
    return document.documentElement.getAttribute('data-theme') === 'dark';
  }

  function ensureHeight() {
    const needed = Math.max(document.body.scrollHeight + 3000, H);
    if (rows < Math.ceil(needed / SPACING) + 2) resize();
  }

  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    const docH = Math.max(document.body.scrollHeight + 3000, H);
    canvas.width = W * DPR;
    canvas.height = H * DPR;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);

    cols = Math.ceil(W / SPACING) + 2;
    rows = Math.ceil(docH / SPACING) + 2;
    grid = [];
    for (let r = 0; r < rows; r++) {
      const row = [];
      for (let c = 0; c < cols; c++) {
        row.push({ x: c * SPACING, y: r * SPACING, dx: 0, dy: 0, vx: 0, vy: 0 });
      }
      grid.push(row);
    }
  }

  // ---- physics ----
  function updatePhysics() {
    // 有效鼠标位置
    if (mX > -9990) {
      effX = mX;
      effY = mY + scrollY;
    }

    // 有效速度（鼠标 + 滚动相对位移）
    if (prevX > -9990 && effX > -9990) {
      speed = Math.min(Math.sqrt((effX - prevX) ** 2 + (effY - prevY) ** 2), 60);
    }
    prevX = effX; prevY = effY;
    speed *= 0.88;
    bottomTap *= 0.78;
    bottomPull *= 0.97;
    topTap *= 0.78;
    topPull *= 0.97;

    updateRipples();

    const copy = [];
    for (let r = 0; r < rows; r++) {
      copy[r] = [];
      for (let c = 0; c < cols; c++) {
        const g = grid[r][c];
        copy[r][c] = { dx: g.dx, dy: g.dy, vx: g.vx, vy: g.vy };
      }
    }

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const g = grid[r][c];
        const co = copy[r][c];

        g.vx += -SPRING_K * co.dx;
        g.vy += -SPRING_K * co.dy;
        g.vx *= DAMPING;
        g.vy *= DAMPING;

        // 底部短促脉冲（快速滚一下，直接拉位移 + 消速）
        if (bottomTap > 0.01) {
          const dy = (scrollY + H) - g.y;
          if (dy > 0 && dy < 40) {
            const t = 1 - dy / 40;
            g.dy = -bottomTap * t * 5;
            g.vy *= 0.3;
          }
        }
        // 顶部短促脉冲
        if (topTap > 0.01) {
          const dy = g.y - scrollY;
          if (dy > 0 && dy < 40) {
            const t = 1 - dy / 40;
            g.dy = topTap * t * 5;
            g.vy *= 0.3;
          }
        }
        // 顶部持续拉伸
        if (topPull > 0.01) {
          const dy = g.y - scrollY;
          if (dy > 0 && dy < 120) {
            g.vy += topPull * (1 - dy / 120) * 0.3;
          }
        }

        // 底部持续拉伸（按住滚持续下滑）
        if (bottomPull > 0.01) {
          const dy = (scrollY + H) - g.y;
          if (dy > 0 && dy < 120) {
            g.vy -= bottomPull * (1 - dy / 120) * 0.3;
          }
        }

        // 彩蛋：持续崩坏力（从顶部到底部波动，逐帧施加）
        if (chaosActive && chaosEnergy > 0.5) {
          const wave = Math.sin(g.y * 0.015 + g.x * 0.01 + chaosPhase);
          const f = chaosEnergy * (0.8 + wave * 0.6);
          g.vx += Math.sin(g.y * 0.01 + chaosPhase) * f * 0.8;
          g.vy += (wave * 0.6 + (Math.random() - 0.5) * 0.4) * f;
        }

        // 鼠标径向推离
        if (effX > -9990 && speed > 1) {
          const dx = g.x - effX;
          const dy = g.y - effY;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < MOUSE_RADIUS && dist > 0.5) {
            const t = 1 - dist / MOUSE_RADIUS;
            const strength = Math.min(speed / 25, 4) * t * t * 1.2;
            g.vx += (dx / dist) * strength;
            g.vy += (dy / dist) * strength;
          }
        }

        if (r > 0) { g.vx += (copy[r-1][c].dx - co.dx) * COUPLING; g.vy += (copy[r-1][c].dy - co.dy) * COUPLING; }
        if (r < rows-1) { g.vx += (copy[r+1][c].dx - co.dx) * COUPLING; g.vy += (copy[r+1][c].dy - co.dy) * COUPLING; }
        if (c > 0) { g.vx += (copy[r][c-1].dx - co.dx) * COUPLING; g.vy += (copy[r][c-1].dy - co.dy) * COUPLING; }
        if (c < cols-1) { g.vx += (copy[r][c+1].dx - co.dx) * COUPLING; g.vy += (copy[r][c+1].dy - co.dy) * COUPLING; }

        g.dx += g.vx;
        g.dy += g.vy;
      }
    }

    // 崩坏能量持续增长到上限后稳定
    if (chaosActive) {
      chaosPhase += 0.03;
      if (chaosEnergy < 40) chaosEnergy = Math.min(chaosEnergy + 0.2, 40);
    }

    prevScrollY = scrollY;
  }

  function getEnergy(dx, dy, vx, vy) {
    return Math.min(1, Math.sqrt(dx * dx + dy * dy) * 0.6 + Math.sqrt(vx * vx + vy * vy) * 3);
  }

  // ---- click ripple ----
  const MAX_RIPPLE_RADIUS = isMobile ? 80 : 140;
  const RIPPLE_DURATION = isMobile ? 400 : 500;

  function addRipple(x, y) {
    ripples.push({ x, y, startTime: performance.now() });
  }

  function updateRipples() {
    const now = performance.now();
    ripples = ripples.filter(r => now - r.startTime < RIPPLE_DURATION);
    if (!ripples.length) return;

    for (const rip of ripples) {
      const age = now - rip.startTime;
      const progress = age / RIPPLE_DURATION;
      const radius = progress * MAX_RIPPLE_RADIUS;
      const ringWidth = SPACING * 2;
      const strength = (1 - progress) * 3.5;

      // 只处理涟漪影响范围内的网格点
      const margin = Math.ceil((radius + ringWidth) / SPACING) + 1;
      const col0 = Math.max(0, Math.floor(rip.x / SPACING) - margin);
      const col1 = Math.min(cols - 1, Math.ceil(rip.x / SPACING) + margin);
      const row0 = Math.max(0, Math.floor(rip.y / SPACING) - margin);
      const row1 = Math.min(rows - 1, Math.ceil(rip.y / SPACING) + margin);

      for (let r = row0; r <= row1; r++) {
        for (let c = col0; c <= col1; c++) {
          const g = grid[r][c];
          const dx = g.x - rip.x;
          const dy = g.y - rip.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const distFromRing = Math.abs(dist - radius);
          if (distFromRing < ringWidth && dist > 0.5) {
            const t = 1 - distFromRing / ringWidth;
            const force = strength * t * t;
            g.vx += (dx / dist) * force;
            g.vy += (dy / dist) * force;
          }
        }
      }
    }
  }

  // ---- draw ----
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    updatePhysics();

    let alive = false;
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const g = grid[r][c];
        if (Math.abs(g.dx) > 0.2 || Math.abs(g.dy) > 0.2 ||
            Math.abs(g.vx) > 0.05 || Math.abs(g.vy) > 0.05) {
          alive = true; break;
        }
      }
      if (alive) break;
    }
    if (!alive) return;

    const [ar, ag, ab] = getAccent();
    const maxAlpha = isDark() ? (isMobile ? 0.12 : 0.3) : (isMobile ? 0.25 : 0.6);
    const sy = scrollY;
    const vpBottom = sy + H;

    for (let r = 0; r < rows; r++) {
      const rowY = grid[r][0].y;
      if (rowY - sy > vpBottom + SPACING) break;
      if (rowY - sy < -SPACING) continue;
      for (let c = 0; c < cols - 1; c++) {
        const g1 = grid[r][c], g2 = grid[r][c + 1];
        const e = Math.max(getEnergy(g1.dx, g1.dy, g1.vx, g1.vy),
                           getEnergy(g2.dx, g2.dy, g2.vx, g2.vy));
        if (e < 0.005) continue;
        ctx.beginPath();
        ctx.moveTo(g1.x + g1.dx, g1.y + g1.dy - sy);
        ctx.lineTo(g2.x + g2.dx, g2.y + g2.dy - sy);
        ctx.strokeStyle = `rgba(${ar},${ag},${ab},${e * maxAlpha})`;
        ctx.lineWidth = 0.8;
        ctx.stroke();
      }
    }

    for (let c = 0; c < cols; c++) {
      for (let r = 0; r < rows - 1; r++) {
        const g1 = grid[r][c], g2 = grid[r + 1][c];
        const gy1 = g1.y - sy, gy2 = g2.y - sy;
        if (gy1 > vpBottom && gy2 > vpBottom) break;
        if (gy1 < -SPACING && gy2 < -SPACING) continue;
        const e = Math.max(getEnergy(g1.dx, g1.dy, g1.vx, g1.vy),
                           getEnergy(g2.dx, g2.dy, g2.vx, g2.vy));
        if (e < 0.005) continue;
        ctx.beginPath();
        ctx.moveTo(g1.x + g1.dx, gy1 + g1.dy);
        ctx.lineTo(g2.x + g2.dx, gy2 + g2.dy);
        ctx.strokeStyle = `rgba(${ar},${ag},${ab},${e * maxAlpha})`;
        ctx.lineWidth = 0.8;
        ctx.stroke();
      }
    }
  }

  function animate() {
    draw();
    animId = requestAnimationFrame(animate);
  }

  // ---- events ----
  window.addEventListener('scroll', () => {
    scrollY = window.scrollY;
  }, { passive: true });

  window.addEventListener('wheel', (e) => {
    const maxSY = Math.max(document.body.scrollHeight - window.innerHeight, 0);
    const now = performance.now();
    const isContinuous = (now - lastWheelTime < 120);
    lastWheelTime = now;

    if (e.deltaY > 0 && scrollY >= maxSY - 1) {
      bottomTap = Math.min(bottomTap + e.deltaY * 0.03, 3);
      if (isContinuous) bottomPull = Math.min(bottomPull + e.deltaY * 0.012, 2);
    }
    if (e.deltaY < 0 && scrollY <= 1) {
      const force = -e.deltaY;  // deltaY < 0, force > 0
      topTap = Math.min(topTap + force * 0.03, 3);
      if (isContinuous) topPull = Math.min(topPull + force * 0.012, 2);
    }
  }, { passive: true });

  document.addEventListener('mousemove', (e) => {
    mX = e.clientX; mY = e.clientY;
  });
  document.addEventListener('mouseleave', () => { mX = -9999; mY = -9999; speed = 0; });
  document.addEventListener('touchmove', (e) => {
    const t = e.touches[0];
    if (t) { mX = t.clientX; mY = t.clientY; }
  }, { passive: true });

  // ---- click/tap ripple ----
  function isInteractive(el) {
    if (!el) return true;
    const tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.closest('.blog-card') || el.closest('.back-to-top') || el.closest('.footer__refresh')) return true;
    if (el.closest('#blog-navbar') || el.closest('.giscus, .giscus-frame')) return true;
    return false;
  }

  document.addEventListener('click', (e) => {
    if (isInteractive(e.target)) return;
    addRipple(e.clientX, e.clientY + scrollY);
  });

  document.addEventListener('touchstart', (e) => {
    if (isInteractive(e.target)) return;
    const t = e.changedTouches[0];
    if (t) addRipple(t.clientX, t.clientY + scrollY);
  }, { passive: true });

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resize, 200);
  });
  const ro = new ResizeObserver(() => {
    const needed = Math.max(document.body.scrollHeight + 3000, window.innerHeight);
    if (rows < Math.ceil(needed / SPACING) + 2) resize();
  });
  ro.observe(document.body);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden && animId) { cancelAnimationFrame(animId); animId = null; }
    else if (!document.hidden && !animId) { animate(); }
  });

  // ===== 彩蛋：崩坏模式 =====
  window.__bgEasterEgg = function() {
    // 初始剧烈撕裂
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const g = grid[r][c];
        const angle = Math.random() * Math.PI * 2;
        const dist = 200 + Math.random() * 500;
        g.dx = Math.cos(angle) * dist;
        g.dy = Math.sin(angle) * dist;
        g.vx = Math.cos(angle) * (30 + Math.random() * 40);
        g.vy = Math.sin(angle) * (30 + Math.random() * 40);
      }
    }
    // 行/列撕裂
    for (let strip = 0; strip < 10; strip++) {
      if (Math.random() > 0.5) {
        const r = Math.floor(Math.random() * rows);
        const off = 100 + Math.random() * 300;
        for (let c = 0; c < cols; c++) {
          grid[r][c].dx += off * (Math.random() - 0.5);
          grid[r][c].dy += off * (Math.random() - 0.5);
        }
      } else {
        const c = Math.floor(Math.random() * cols);
        const off = 100 + Math.random() * 300;
        for (let r = 0; r < rows; r++) {
          grid[r][c].dx += off * (Math.random() - 0.5);
          grid[r][c].dy += off * (Math.random() - 0.5);
        }
      }
    }
    // 激活持续崩坏（能量从 0 开始逐帧增长，约 5 秒达到峰值 100）
    chaosActive = true;
  };

  resize();
  animate();
})();
