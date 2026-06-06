(function() {
  // 工具函数：十六进制转 RGB
  function hexToRgb(hex) {
    let c = hex.substring(1);
    if (c.length === 3) c = c.split('').map(ch => ch + ch).join('');
    const intVal = parseInt(c, 16);
    return [(intVal >> 16) & 255, (intVal >> 8) & 255, intVal & 255].join(',');
  }

  function setFavicon(iconPath) {
    const existing = document.querySelector('link[rel="icon"]');
    if (existing) existing.remove();
    const link = document.createElement('link');
    link.rel = 'icon';
    link.type = 'image/svg+xml';
    link.href = iconPath;
    document.head.appendChild(link);
  }

  document.addEventListener('DOMContentLoaded', async function() {
    if (document.getElementById('blog-navbar')) return;

    let navbarConfig;
    try {
      // console.log('[navbar.js] 正在加载配置文件：/json/config.json');
      const res = await fetch('/json/config.json');
      if (!res.ok) {
        console.warn(`[navbar.js] 配置文件加载失败，状态码：${res.status}`);
        throw new Error(`HTTP ${res.status}`);
      }
      const config = await res.json();
      // console.log('[navbar.js] 配置文件加载成功：', config);
      
      // 将模块化配置转换为navbar.js期望的扁平结构
      navbarConfig = {
        themeColor: config.theme?.color || "#399FFF",
        navbarHeight: config.navbar?.height || 60,
        navbarHoverHeight: config.navbar?.hoverHeight || 80,
        logo: config.navbar?.logo || {
          src: "/images/icon.svg",
          alt: "ReOri Logo",
          fixedHeight: "24px",
          textFallback: "ReOri Logo[Load Failed]"
        },
        siteName: config.site?.name || "ReOri",
        navItems: config.navigation?.items || [{ name: "首页", url: "/index.html" }]
      };
    } catch (e) {
      console.warn('[navbar.js] 加载配置文件失败，使用默认配置:', e);
      // 加载失败时使用默认配置
      navbarConfig = {
        themeColor: "#399FFF",
        navbarHeight: 60,
        navbarHoverHeight: 80,
        logo: {
          src: "/images/icon.svg",
          alt: "ReOri Logo",
          fixedHeight: "24px",
          textFallback: "ReOri Logo[Load Failed]"
        },
        siteName: "ReOri",
        navItems: [{ name: "首页", url: "/index.html" }]
      };
    }

    // 设置默认主题CSS 变量
    const themeColor = navbarConfig.themeColor || "#399FFF";
    document.documentElement.style.setProperty('--theme-color', themeColor);
    document.documentElement.style.setProperty('--theme-color-rgb', hexToRgb(themeColor));

    // 设置 favicon
    if (navbarConfig.logo && navbarConfig.logo.src) {
      setFavicon(navbarConfig.logo.src);
    }

    const navbarHeight = navbarConfig.navbarHeight || 60;
    const navbarHoverHeight = navbarConfig.navbarHoverHeight || 80;
    const iconHeight = parseInt(navbarConfig.logo.fixedHeight) || 24;

    let spacing = (navbarHeight - iconHeight) / 2;
    if (spacing < 0) {
      spacing = iconHeight * 0.2;
      console.warn('导航栏原始高度过小，已使用图标高度的20%作为间距');
    }

    const hoverIconHeight = navbarHoverHeight - 2 * spacing;
    const iconScale = hoverIconHeight / iconHeight;
    const offset = hoverIconHeight - iconHeight;

    // 动态样式（使用 CSS 变量）
    const style = document.createElement('style');
    style.textContent = `
      #blog-navbar {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        height: ${navbarHeight}px;
        background: rgba(255,255,255, 0.72);
        backdrop-filter: blur(12px);
        border-bottom-left-radius: 8px;
        border-bottom-right-radius: 8px;
        box-shadow: 0 2px 12px rgba(0,0,0,0.08);
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        padding: 0 ${spacing}px;
        z-index: 9999;
        transition: height 0.4s cubic-bezier(0.3, 0.9, 0.2, 1) !important;
        overflow: hidden;
      }
      #blog-navbar:hover {
        height: ${navbarHoverHeight}px;
      }
      .navbar-logo-wrap {
        display: flex;
        align-items: flex-start;
        height: 100%;
        flex-shrink: 0;
        cursor: pointer;
        gap: ${spacing}px;
        padding-left: ${spacing}px;
        padding-top: ${spacing}px;
        transition: all 0.4s cubic-bezier(0.3, 0.9, 0.2, 1) !important;
        align-self: flex-start;
      }
      .navbar-logo,
      .navbar-logo-text {
        height: ${iconHeight}px;
        width: auto;
        display: block;
        transform-origin: left top;
        transition: transform 0.4s cubic-bezier(0.3, 0.9, 0.2, 1) !important;
        flex-shrink: 0;
      }
      #blog-navbar:hover .navbar-logo,
      #blog-navbar:hover .navbar-logo-text {
        transform: scale(${iconScale});
      }
      .navbar-logo-text {
        font-size: 14px;
        font-weight: 600;
        color: var(--theme-color, #399FFF);
        line-height: ${iconHeight}px;
      }
      .copy-url-btn {
        background: transparent;
        border: none;
        border-radius: 6px;
        cursor: pointer;
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        height: ${iconHeight}px;
        width: ${iconHeight}px;
        margin: 0;
        padding: 0;
        transition: transform 0.4s cubic-bezier(0.3, 0.9, 0.2, 1) !important;
        transform: translate(0, 0);
      }
      #blog-navbar:hover .copy-url-btn {
        transform: translate(${offset}px, ${offset}px);
      }
      .copy-url-btn img {
        height: ${iconHeight}px;
        width: auto;
        opacity: 0.7;
        transform: none !important;
        transition: opacity 0.2s !important;
      }
      .copy-url-btn:hover img {
        opacity: 1;
      }
      .navbar-right-container {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        height: 100%;
        gap: ${spacing}px;
        padding-right: ${spacing}px;
        padding-top: 0;
        align-self: flex-end;
        transition: padding-top 0.4s cubic-bezier(0.3, 0.9, 0.2, 1) !important;
      }
      #blog-navbar:hover .navbar-right-container {
        padding-top: ${offset}px;
      }
      .navbar-nav {
        display: flex;
        list-style: none;
        gap: ${spacing}px;
        margin: 0;
        padding: 0;
      }
      .navbar-nav a {
        text-decoration: none;
        color: #333;
        padding: 6px 10px;
        border-radius: 6px;
        white-space: nowrap;
        transition: color 0.2s, background 0.2s;
      }
      .navbar-nav a:hover {
        background: rgba(var(--theme-color-rgb, 57,159,255), 0.12);
        color: var(--theme-color, #399FFF);
      }
      .navbar-sitename {
        font-size: 16px;
        font-weight: 700;
        color: var(--theme-color, #399FFF);
        white-space: nowrap;
        padding: 6px 10px;
        cursor: pointer;
        flex-shrink: 0;
      }
      body {
        padding-top: ${navbarHeight + 20}px !important;
      }
      #progress-container {
        position: fixed;
        left: 0;
        bottom: 0;
        width: 100%;
        height: 6px;
        background: rgba(210,228,255,0.9);
        backdrop-filter: blur(8px);
        border-top: 1px solid rgba(var(--theme-color-rgb,57,159,255),0.12);
        z-index: 9998;
        transition: height 0.25s cubic-bezier(0.2, 0.9, 0.3, 1),
                    background 0.25s ease;
      }
      #progress-container:hover,
      #progress-container.dragging {
        height: 18px;
        background: rgba(200,220,250,0.95);
        box-shadow: 0 -6px 20px 2px rgba(var(--theme-color-rgb,57,159,255),0.35);
      }
      #read-progress {
        height: 100%;
        width: 0%;
        background: var(--theme-color, #399FFF);
        border-top-right-radius: 3px;
        border-bottom-right-radius: 3px;
        transition: width 0.1s linear;
      }
      .copy-toast {
        position: fixed;
        bottom: 20px;
        right: 20px;
        background: rgba(var(--theme-color-rgb, 57,159,255), 0.9);
        color: white;
        padding: 8px 16px;
        border-radius: 6px;
        font-size: 14px;
        z-index: 10000;
        opacity: 0;
        transform: translateX(20px);
        transition: opacity 0.3s cubic-bezier(0.68, -0.55, 0.27, 1.55), transform 0.3s cubic-bezier(0.68, -0.55, 0.27, 1.55);
        pointer-events: none;
        white-space: nowrap;
        max-width: 80vw;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .copy-toast.show {
        opacity: 1;
        transform: translateX(0);
      }
      .copy-toast .toast-address {
        color: rgba(255,255,255,0.7);
        margin-right: 4px;
      }

      /* ===== Hamburger Menu Button ===== */
      .navbar-hamburger {
        display: none;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        width: 36px;
        height: 36px;
        background: none;
        border: none;
        cursor: pointer;
        padding: 6px;
        border-radius: 6px;
        color: #333;
        transition: background 0.2s;
        flex-shrink: 0;
        gap: 5px;
      }
      .navbar-hamburger:hover {
        background: rgba(var(--theme-color-rgb, 57,159,255), 0.1);
      }
      .navbar-hamburger span {
        display: block;
        width: 20px;
        height: 2px;
        background: currentColor;
        border-radius: 2px;
        transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1),
                    opacity 0.3s ease;
      }
      .navbar-hamburger span:nth-child(2) {
        width: 14px;
        align-self: center;
      }
      #blog-navbar.nav-open .navbar-hamburger span:nth-child(1) {
        transform: rotate(45deg) translate(4px, 4px);
      }
      #blog-navbar.nav-open .navbar-hamburger span:nth-child(2) {
        opacity: 0;
      }
      #blog-navbar.nav-open .navbar-hamburger span:nth-child(3) {
        transform: rotate(-45deg) translate(4px, -4px);
      }

      /* ===== Mobile Dropdown Menu ===== */
      .navbar-mobile-menu {
        display: none;
        position: fixed;
        top: ${navbarHeight}px;
        left: 8px;
        right: 8px;
        background: rgba(255,255,255,0.96);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border-radius: 14px;
        box-shadow: 0 8px 40px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.06);
        z-index: 9998;
        padding: 6px 0;
        transform: translateY(-16px);
        opacity: 0;
        transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1),
                    opacity 0.2s ease;
        pointer-events: none;
        overflow: hidden;
      }
      .navbar-mobile-menu.open {
        transform: translateY(0);
        opacity: 1;
        pointer-events: auto;
      }
      .navbar-mobile-menu .nav-mobile-header {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 16px 20px 12px;
        border-bottom: 1px solid rgba(0,0,0,0.06);
        margin-bottom: 4px;
      }
      .navbar-mobile-menu .nav-mobile-header img {
        height: 28px;
        width: auto;
        flex-shrink: 0;
      }
      .navbar-mobile-menu .nav-mobile-header .nav-mobile-title {
        font-size: 17px;
        font-weight: 700;
        color: var(--theme-color, #399FFF);
        cursor: pointer;
        flex: 1;
      }
      .navbar-mobile-menu a {
        display: flex;
        align-items: center;
        padding: 14px 20px;
        color: #333;
        text-decoration: none;
        font-size: 16px;
        margin: 0 6px;
        border-radius: 10px;
        transition: background 0.15s;
        -webkit-tap-highlight-color: transparent;
        position: relative;
      }
      .navbar-mobile-menu a:hover {
        background: rgba(var(--theme-color-rgb, 57,159,255), 0.08);
        color: var(--theme-color, #399FFF);
      }
      .navbar-mobile-menu a:active {
        background: rgba(var(--theme-color-rgb, 57,159,255), 0.15);
        transform: scale(0.98);
      }

      /* ===== Mobile Overlay ===== */
      .navbar-mobile-overlay {
        display: none;
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.18);
        backdrop-filter: blur(2px);
        -webkit-backdrop-filter: blur(2px);
        z-index: 9997;
        opacity: 0;
        transition: opacity 0.25s ease;
        pointer-events: none;
      }
      .navbar-mobile-overlay.visible {
        opacity: 1;
        pointer-events: auto;
      }

      /* Dark mode overrides */
      [data-theme="dark"] .navbar-hamburger {
        color: #a0a0b8;
      }
      [data-theme="dark"] .navbar-hamburger:hover {
        background: rgba(57,159,255,0.15);
        color: var(--theme-color, #399FFF);
      }
      [data-theme="dark"] .navbar-mobile-menu {
        background: rgba(30,32,52,0.96);
        box-shadow: 0 8px 40px rgba(0,0,0,0.4), 0 2px 8px rgba(0,0,0,0.2);
      }
      [data-theme="dark"] .navbar-mobile-menu a {
        color: #a0a0b8;
      }
      [data-theme="dark"] .navbar-mobile-menu a:hover {
        background: rgba(57,159,255,0.12);
        color: var(--theme-color, #399FFF);
      }
      [data-theme="dark"] .navbar-mobile-menu .nav-mobile-header {
        border-bottom-color: rgba(255,255,255,0.06);
      }
      [data-theme="dark"] .navbar-mobile-overlay {
        background: rgba(0,0,0,0.4);
      }
      [data-theme="dark"] .copy-url-btn img {
        filter: brightness(0) invert(0.72);
        opacity: 0.8;
      }
      [data-theme="dark"] .copy-url-btn:hover img {
        filter: brightness(0) invert(0.85);
        opacity: 1;
      }

      /* 拖拽进度条时禁用卡片 hover 动画，避免卡顿 */
      body.is-dragging .blog-card {
        transition: none !important;
        transform: none !important;
      }
      body.is-dragging .blog-card::before,
      body.is-dragging .blog-card::after {
        transition: none !important;
        animation: none !important;
        opacity: 0 !important;
        transform: scaleX(0) !important;
      }
      body.is-dragging .blog-card:hover {
        transform: none !important;
        box-shadow: none !important;
      }
      body.is-dragging .blog-card:hover::before,
      body.is-dragging .blog-card:hover::after {
        opacity: 0 !important;
        transform: scaleX(0) !important;
      }
      body.is-dragging .blog-card:hover .blog-card__cover img {
        transform: none !important;
      }

      /* ===== Responsive Navbar ===== */
      @media (max-width: 768px) {
        .navbar-hamburger {
          display: flex;
        }
        .navbar-nav {
          display: none !important;
        }
        .navbar-sitename {
          display: none !important;
        }
        .navbar-mobile-menu {
          display: block;
        }
        .navbar-mobile-overlay {
          display: block;
        }
        #blog-navbar {
          border-bottom-left-radius: 0;
          border-bottom-right-radius: 0;
        }
      }
    `;
    document.head.append(style);

    // 创建复制提示元素
    const toast = document.createElement('div');
    toast.className = 'copy-toast';
    document.body.appendChild(toast);

    // 通用弹窗函数（与复制弹窗样式一致）
    window.showToast = function(text, duration = 2000) {
      toast.textContent = text;
      toast.style.textAlign = 'center';
      toast.style.whiteSpace = 'normal';
      toast.classList.add('show');
      clearTimeout(toast._hideTimer);
      toast._hideTimer = setTimeout(() => toast.classList.remove('show'), duration);
    };

    const copyToClipboard = (text) => {
      navigator.clipboard.writeText(text).then(() => {
        const display = text.length > 50 ? text.slice(0, 47) + '...' : text;
        window.showToast(display + ' 已复制', 1500);
      }).catch(err => {
        console.error('复制失败:', err);
        window.showToast('复制失败！', 1500);
      });
    };

    // 构建导航栏DOM
    const nav = document.createElement('nav');
    nav.id = 'blog-navbar';

    const logoWrap = document.createElement('div');
    logoWrap.className = 'navbar-logo-wrap';
    logoWrap.addEventListener('click', (e) => {
      if (!e.target.closest('.copy-url-btn')) {
        window.location.href = '/index.html';
      }
    });

    const img = document.createElement('img');
    img.className = 'navbar-logo';
    img.src = navbarConfig.logo.src;
    img.alt = navbarConfig.logo.alt;
    const txt = document.createElement('div');
    txt.className = 'navbar-logo-text';
    txt.textContent = navbarConfig.logo.textFallback;
    img.onerror = () => { img.remove(); logoWrap.append(txt); };

    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-url-btn';
    copyBtn.title = '复制当前地址';
    const copyIcon = document.createElement('img');
    copyIcon.src = '/images/copy.svg';
    copyIcon.alt = '复制';
    copyIcon.style.height = `${iconHeight}px`;
    copyBtn.appendChild(copyIcon);
    copyBtn.addEventListener('click', () => {
      copyToClipboard(window.location.href);
    });

    logoWrap.append(img, copyBtn);

    const rightContainer = document.createElement('div');
    rightContainer.className = 'navbar-right-container';

    // 深色模式切换按钮
    const themeBtn = document.createElement('button');
    themeBtn.className = 'navbar-theme-btn';
    themeBtn.title = '切换深色模式';
    themeBtn.innerHTML = `
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
        <path class="sun" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"/>
        <path class="moon" d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" style="display:none"/>
      </svg>
    `;
    themeBtn.style.cssText = `
      background: none; border: none; cursor: pointer; padding: 6px;
      color: #333; border-radius: 6px; display: flex; align-items: center;
      transition: color 0.2s, background 0.2s; flex-shrink: 0;
    `;

    // 初始化深色模式
    const saved = localStorage.getItem('reori-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = saved ? saved === 'dark' : prefersDark;
    if (isDark) document.documentElement.setAttribute('data-theme', 'dark');
    updateThemeIcon();

    function updateThemeIcon() {
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      const sun = themeBtn.querySelector('.sun');
      const moon = themeBtn.querySelector('.moon');
      if (sun) sun.style.display = dark ? '' : 'none';
      if (moon) moon.style.display = dark ? 'none' : '';
    }

    // 主题切换 overlay
    let themeOverlay = null;
    function getThemeOverlay() {
      if (!themeOverlay) {
        themeOverlay = document.createElement('div');
        themeOverlay.id = 'theme-transition-overlay';
        document.body.appendChild(themeOverlay);
      }
      return themeOverlay;
    }

    // 彩蛋：快速连切深色模式
    let themeClicks = [];
    let easterEggCooldown = false;
    const THEME_EASTER_EGG_COUNT = 10;
    const THEME_EASTER_EGG_WINDOW = 2000;

    function checkThemeEasterEgg() {
      const now = Date.now();
      themeClicks.push(now);
      themeClicks = themeClicks.filter(t => now - t < THEME_EASTER_EGG_WINDOW);
      if (themeClicks.length >= THEME_EASTER_EGG_COUNT) {
        themeClicks = [];
        easterEggCooldown = true;
        if (typeof window.__bgEasterEgg === 'function') window.__bgEasterEgg();
        showEasterEggEmoji();
        setTimeout(() => { easterEggCooldown = false; }, 4000);
      }
    }

    function showEasterEggEmoji() {
      window.showToast('(・_・?)', 3000);
    }

    themeBtn.addEventListener('click', () => {
      if (easterEggCooldown) {
        showEasterEggEmoji();
        return;
      }
      const now = document.documentElement.getAttribute('data-theme') === 'dark';
      const targetDark = !now;
      const overlay = getThemeOverlay();

      // 静默重置：关掉 transition，瞬间归位
      overlay.style.transition = 'none';
      overlay.style.transform = 'translate(-50%, -50%) scale(0)';
      overlay.style.opacity = '1';
      overlay.style.background = targetDark ? '#13141f' : '#f5f7fa';
      const rect = themeBtn.getBoundingClientRect();
      overlay.style.left = (rect.left + rect.width / 2) + 'px';
      overlay.style.top = (rect.top + rect.height / 2) + 'px';

      // 强制回流，确保重置生效
      overlay.offsetHeight;

      // 恢复 transition，开始展开
      overlay.style.transition = '';
      overlay.style.transform = 'translate(-50%, -50%) scale(1)';

      // 圆形覆盖大半时切换主题 + 淡出
      setTimeout(() => {
        document.documentElement.setAttribute('data-theme', targetDark ? 'dark' : '');
        localStorage.setItem('reori-theme', targetDark ? 'dark' : 'light');
        updateThemeIcon();
        overlay.style.opacity = '0';
        // 主题切换完成后检测彩蛋，确保崩坏时的背景颜色与切换后一致
        checkThemeEasterEgg();
      }, 200);
    });

    const ul = document.createElement('ul');
    ul.className = 'navbar-nav';
    (Array.isArray(navbarConfig.navItems) ? navbarConfig.navItems : []).forEach(item => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = item.url || '#';
      a.textContent = item.name;
      li.appendChild(a);
      ul.appendChild(li);
    });

    const siteName = document.createElement('div');
    siteName.className = 'navbar-sitename';
    siteName.textContent = navbarConfig.siteName;
    siteName.addEventListener('click', () => {
      window.location.href = '/index.html';
    });

    // 移动端汉堡菜单按钮
    const hamburgerBtn = document.createElement('button');
    hamburgerBtn.className = 'navbar-hamburger';
    hamburgerBtn.setAttribute('aria-label', '菜单');
    hamburgerBtn.innerHTML = '<span></span><span></span><span></span>';

    rightContainer.append(themeBtn, ul, siteName, hamburgerBtn);
    nav.append(logoWrap, rightContainer);

    document.body.insertBefore(nav, document.body.firstChild);

    // === 移动端下拉菜单 ===
    const mobileMenu = document.createElement('div');
    mobileMenu.className = 'navbar-mobile-menu';

    const mobHeader = document.createElement('div');
    mobHeader.className = 'nav-mobile-header';
    const mobLogo = document.createElement('img');
    mobLogo.src = navbarConfig.logo.src;
    mobLogo.alt = navbarConfig.logo.alt;
    mobLogo.loading = 'lazy';
    const mobTitle = document.createElement('span');
    mobTitle.className = 'nav-mobile-title';
    mobTitle.textContent = navbarConfig.siteName;
    mobTitle.addEventListener('click', () => { window.location.href = '/index.html'; });
    mobHeader.append(mobLogo, mobTitle);
    mobileMenu.appendChild(mobHeader);

    ul.querySelectorAll('a').forEach(a => {
      const link = a.cloneNode(true);
      link.addEventListener('click', closeMobileMenu);
      mobileMenu.appendChild(link);
    });

    const overlay = document.createElement('div');
    overlay.className = 'navbar-mobile-overlay';
    overlay.addEventListener('click', closeMobileMenu);

    function closeMobileMenu() {
      nav.classList.remove('nav-open');
      mobileMenu.classList.remove('open');
      overlay.classList.remove('visible');
    }

    hamburgerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = mobileMenu.classList.contains('open');
      nav.classList.toggle('nav-open');
      mobileMenu.classList.toggle('open');
      overlay.classList.toggle('visible');
    });

    document.body.appendChild(mobileMenu);
    document.body.appendChild(overlay);

    // 进度条
    const progressContainer = document.createElement('div');
    progressContainer.id = 'progress-container';
    const progressBar = document.createElement('div');
    progressBar.id = 'read-progress';
    progressContainer.append(progressBar);
    document.body.appendChild(progressContainer);

    let scrollTicking = false;
    function updateProgress() {
      const scrollTop = window.scrollY;
      const scrollHeight = document.documentElement.scrollHeight - window.innerHeight;
      const percent = scrollHeight > 0 ? (scrollTop / scrollHeight) * 100 : 0;
      progressBar.style.width = percent + '%';
    }
    window.addEventListener('scroll', () => {
      if (!scrollTicking) {
        requestAnimationFrame(() => {
          updateProgress();
          scrollTicking = false;
        });
        scrollTicking = true;
      }
    });
    updateProgress();

    // 回到顶部按钮
    const backBtn = document.createElement('button');
    backBtn.className = 'back-to-top';
    backBtn.title = '回到顶部';
    backBtn.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="18 15 12 9 6 15"/>
      </svg>
    `;
    document.body.appendChild(backBtn);

    let backBtnTicking = false;
    function updateBackToTop() {
      backBtn.classList.toggle('visible', window.scrollY > 320);
    }
    window.addEventListener('scroll', () => {
      if (!backBtnTicking) {
        requestAnimationFrame(() => {
          updateBackToTop();
          backBtnTicking = false;
        });
        backBtnTicking = true;
      }
    });

    backBtn.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    // 进度条拖拽跳转
    let isDragging = false;
    let dragRect = null;
    let dragMaxScroll = 0;

    progressContainer.addEventListener('mousedown', (e) => {
      e.preventDefault();
      isDragging = true;
      document.body.classList.add('is-dragging');
      dragRect = progressContainer.getBoundingClientRect();
      dragMaxScroll = document.documentElement.scrollHeight - window.innerHeight;
      progressContainer.classList.add('dragging');
      progressContainer.style.cursor = 'grabbing';
      seekProgress(e.clientX);
    });

    function seekProgress(clientX) {
      const ratio = Math.max(0, Math.min(1, (clientX - dragRect.left) / dragRect.width));
      window.scrollTo(0, ratio * dragMaxScroll);
    }

    document.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      seekProgress(e.clientX);
    });

    function endDrag() {
      if (!isDragging) return;
      isDragging = false;
      document.body.classList.remove('is-dragging');
      dragRect = null;
      dragMaxScroll = 0;
      updateProgress();
      progressContainer.classList.remove('dragging');
      progressContainer.style.cursor = '';
    }

    window.addEventListener('mouseup', endDrag);
    window.addEventListener('blur', endDrag);

    progressContainer.addEventListener('touchstart', (e) => {
      isDragging = true;
      document.body.classList.add('is-dragging');
      dragRect = progressContainer.getBoundingClientRect();
      dragMaxScroll = document.documentElement.scrollHeight - window.innerHeight;
      progressContainer.classList.add('dragging');
      seekProgress(e.touches[0].clientX);
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
      if (!isDragging) return;
      seekProgress(e.touches[0].clientX);
    }, { passive: true });

    document.addEventListener('touchend', endDrag);
  });
})();