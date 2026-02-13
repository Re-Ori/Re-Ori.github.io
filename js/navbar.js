(function() {
  // 动态设置 favicon
  function setFavicon(iconPath) {
    const existing = document.querySelector('link[rel="icon"]');
    if (existing) existing.remove();
    const link = document.createElement('link');
    link.rel = 'icon';
    link.type = 'image/svg+xml'; // 可根据实际格式调整
    link.href = iconPath;
    document.head.appendChild(link);
  }

  document.addEventListener('DOMContentLoaded', async function() {
    if (document.getElementById('blog-navbar')) return;

    let navbarConfig;
    try {
      const res = await fetch('/data/navbar.json');
      navbarConfig = await res.json();
    } catch (e) {
      navbarConfig = {
        mainSiteUrl: "http://127.0.0.1:5500/",
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

    const style = document.createElement('style');
    style.textContent = `
      /* 导航栏核心容器（保持不变） */
      #blog-navbar {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        height: ${navbarHeight}px;
        background: rgba(255,255,255, 0.55);
        backdrop-filter: blur(16px);
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
        align-items: flex-end;
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
        color: #5ca1ff;
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
        transition: align-items 0.4s cubic-bezier(0.3, 0.9, 0.2, 1) !important;
        align-self: flex-end;
      }
      #blog-navbar:hover .navbar-right-container {
        align-items: flex-end;
        padding-bottom: ${spacing}px;
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
        background: rgba(92,161,255,0.12);
        color: #5ca1ff;
      }
      .navbar-sitename {
        font-size: 16px;
        font-weight: 700;
        color: #5ca1ff;
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
        background: rgba(255,255,255,0.9);
        backdrop-filter: blur(8px);
        z-index: 9998;
      }
      #read-progress {
        height: 100%;
        width: 0%;
        background: #5ca1ff;
        border-top-right-radius: 3px;
        border-bottom-right-radius: 3px;
        transition: width 0.1s linear;
      }
      .blog-card-mini {
        display: inline-flex;
        align-items: center;
        background: rgba(92, 161, 255, 0.1);
        backdrop-filter: blur(4px);
        border-radius: 8px;
        padding: 0.5rem 1rem;
        margin: 0.5rem 0;
        text-decoration: none;
        color: #5ca1ff;
        font-size: 0.9rem;
        transition: all 0.3s ease;
      }
      .blog-card-mini:hover {
        background: rgba(92, 161, 255, 0.2);
      }

      /* ===== 复制提示（弹性动画 + 地址显示） ===== */
      .copy-toast {
        position: fixed;
        bottom: 20px;
        right: 20px;
        background: rgba(92, 161, 255, 0.9);
        color: white;
        padding: 8px 16px;
        border-radius: 6px;
        font-size: 14px;
        z-index: 10000;
        opacity: 0;
        transform: translateX(20px);
        transition: 
          opacity 0.3s cubic-bezier(0.68, -0.55, 0.27, 1.55),
          transform 0.3s cubic-bezier(0.68, -0.55, 0.27, 1.55);
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
      /* 地址部分使用半透明白色 */
      .copy-toast .toast-address {
        color: rgba(255, 255, 255, 0.7);
        margin-right: 4px;
      }
    `;
    document.head.append(style);

    // 创建复制提示元素（带地址span）
    const toast = document.createElement('div');
    toast.className = 'copy-toast';
    toast.innerHTML = '<span class="toast-address"></span><span>复制成功</span>';
    document.body.appendChild(toast);

    const copyToClipboard = (text) => {
      const addressSpan = toast.querySelector('.toast-address');
      addressSpan.textContent = text + ' ';
      navigator.clipboard.writeText(text).then(() => {
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 2000);
      }).catch(err => {
        console.error('复制失败:', err);
        addressSpan.textContent = '复制失败！';
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 2000);
      });
    };

    // 构建导航栏 DOM（以下代码与原逻辑完全相同）
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

    const ul = document.createElement('ul');
    ul.className = 'navbar-nav';
    (Array.isArray(navbarConfig.navItems) ? navbarConfig.navItems : []).forEach(item => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = item.url || '#';
      a.textContent = item.name;
      li.appendChild(a);
      ul.appendChild(a);
    });

    const siteName = document.createElement('div');
    siteName.className = 'navbar-sitename';
    siteName.textContent = navbarConfig.siteName;
    siteName.addEventListener('click', () => {
      window.location.href = '/index.html';
    });

    rightContainer.append(ul, siteName);
    nav.append(logoWrap, rightContainer);

    const container = document.querySelector('.container') || document.body;
    container.firstChild
      ? container.insertBefore(nav, container.firstChild)
      : container.append(nav);

    // 进度条
    const progressContainer = document.createElement('div');
    progressContainer.id = 'progress-container';
    const progressBar = document.createElement('div');
    progressBar.id = 'read-progress';
    progressContainer.append(progressBar);
    document.body.appendChild(progressContainer);

    function updateProgress() {
      const scrollTop = window.scrollY;
      const scrollHeight = document.documentElement.scrollHeight - window.innerHeight;
      const percent = scrollHeight > 0 ? (scrollTop / scrollHeight) * 100 : 0;
      progressBar.style.width = percent + '%';
    }
    window.addEventListener('scroll', updateProgress);
    updateProgress();
  });
})();