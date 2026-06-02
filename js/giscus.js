(function() {
  var config = null;
  var loading = true;
  var giscusLoaded = false;
  var wrapper, container;
  var useProxy = false;  // 是否使用本地代理

  function showLoading() {
    loading = true;
    container.innerHTML = `
      <div class="giscus-placeholder">
        <div class="giscus-spinner"></div>
        <span>评论区加载中...</span>
      </div>
    `;
  }

  function showError(msg) {
    loading = false;
    container.innerHTML = `
      <div class="giscus-placeholder error">
        <span>${msg}</span>
        <span class="retry-link" id="giscus-retry">点击重试</span>
      </div>
    `;
    var retry = document.getElementById('giscus-retry');
    if (retry) {
      retry.addEventListener('click', function() { loadGiscus(); });
    }
  }

  // 独立判断当前主题
  function getTheme() {
    var saved = localStorage.getItem('reori-theme');
    if (saved === 'dark') return 'dark';
    if (saved === 'light') return 'light';
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function sendGiscusMessage(msg) {
    var iframe = document.querySelector('iframe.giscus-frame');
    if (iframe) {
      var target = useProxy ? window.location.origin : 'https://giscus.app';
      iframe.contentWindow.postMessage({ giscus: msg }, target);
    }
  }

  function renderGiscus(theme) {
    while (container.firstChild) container.removeChild(container.firstChild);

    var script = document.createElement('script');
    // 有代理则加载本地 client.js，否则加载 CDN
    script.src = useProxy ? '/js/giscus-client.js' : 'https://giscus.app/client.js';
    script.setAttribute('data-repo', config.repo);
    script.setAttribute('data-repo-id', config.repoId);
    script.setAttribute('data-category', config.category);
    script.setAttribute('data-category-id', config.categoryId);
    script.setAttribute('data-mapping', config.mapping || 'pathname');
    script.setAttribute('data-reactions-enabled', config.reactionsEnabled || '1');
    script.setAttribute('data-emit-metadata', config.emitMetadata || '0');
    script.setAttribute('data-input-position', config.inputPosition || 'bottom');
    script.setAttribute('data-lang', config.lang || 'zh-CN');
    script.setAttribute('data-theme', theme);
    script.setAttribute('crossorigin', 'anonymous');
    script.async = true;

    script.onerror = function() {
      giscusLoaded = false;
      var msg = useProxy
        ? '评论区本地代理加载失败，请检查网络后重试'
        : '评论区加载失败，请检查网络后重试';
      showError(msg);
    };

    var expectedOrigin = useProxy ? window.location.origin : 'https://giscus.app';

    var messageHandler = function(e) {
      if (e.origin === expectedOrigin && e.data && e.data.giscus) {
        loading = false;
        giscusLoaded = true;
        window.removeEventListener('message', messageHandler);
      }
    };
    window.addEventListener('message', messageHandler);

    var timeout = setTimeout(function() {
      if (loading) {
        showError('评论区加载超时，请检查网络后重试');
      }
    }, 15000);

    window.addEventListener('message', function clear() {
      clearTimeout(timeout);
      window.removeEventListener('message', clear);
    });

    container.appendChild(script);
  }

  // 检测后端是否支持代理
  function checkProxy() {
    return fetch('/api/ping', { method: 'GET', cache: 'no-store' })
      .then(function(r) { return r.ok; })
      .catch(function() { return false; });
  }

  function loadGiscus() {
    if (!config) {
      showError('评论区配置缺失，请检查 config.json 中的 giscus 字段');
      return;
    }
    if (!config.repo || !config.repoId || !config.categoryId) {
      showError('评论区未配置完整 (repo/repoId/categoryId)');
      return;
    }

    // 先检测代理，再决定加载方式
    showLoading();
    checkProxy().then(function(proxyAvail) {
      useProxy = proxyAvail;
      if (proxyAvail) {
        console.log('[giscus] 使用本地代理');
      } else {
        console.log('[giscus] 代理不可用，回退到 CDN 直连');
      }
      renderGiscus(getTheme());
    });
  }

  function init() {
    var contentEl = document.querySelector('.blog-content') || document.getElementById('write');
    if (!contentEl) return;

    wrapper = document.createElement('div');
    wrapper.className = 'giscus-wrapper';
    container = document.createElement('div');
    container.className = 'giscus';
    wrapper.appendChild(container);
    contentEl.parentNode.insertBefore(wrapper, contentEl.nextSibling);

    showLoading();

    fetch('/json/config.json')
      .then(function(res) { return res.json(); })
      .then(function(cfg) {
        config = cfg.giscus || null;
        loadGiscus();

        var observer = new MutationObserver(function(mutations) {
          mutations.forEach(function(m) {
            if (m.attributeName === 'data-theme') {
              if (giscusLoaded) {
                sendGiscusMessage({ setConfig: { theme: getTheme() } });
              }
            }
          });
        });
        observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      })
      .catch(function(e) {
        console.warn('[giscus] 加载配置失败:', e);
        showError('配置加载失败，请刷新页面重试');
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
