// 工具函数：解析 URL 参数
function getUrlParams() {
  const params = new URLSearchParams(window.location.search);
  const result = {};
  for (const [key, value] of params.entries()) {
    result[key] = value;
  }
  return result;
}

// 工具函数：格式化主站地址（兼容末尾有无 /）
function formatMainSiteUrl(url) {
  return url.endsWith('/') ? url : url + '/';
}

// 工具函数：十六进制颜色转 RGB 字符串
function hexToRgb(hex) {
  let c = hex.substring(1);
  if (c.length === 3) c = c.split('').map(ch => ch + ch).join('');
  const intVal = parseInt(c, 16);
  return [(intVal >> 16) & 255, (intVal >> 8) & 255, intVal & 255].join(',');
}

// 博客渲染类
class BlogCardRenderer {
  constructor() {
    this.mainSiteUrl = "";
    this.currentBlogList = [];
    this.currentListUrl = '/blogs/blogs.json';
    this.listName = 'ReOri Blog';
    this.listDescription = '';       // 博客集合的描述
    this.currentSentenceText = '';   // 存储当前一言文本，用于复制
    this.loadingError = false;       // 列表加载失败标志
    this.searchText = '';            // 搜索文本
    this.activeTags = [];            // 当前选中的标签列表（多选）
    this.filterMode = 'or';          // 标签筛选模式: 'or'(任意匹配) / 'and'(全部匹配)
    this.hasInitialRender = false;   // 首次渲染完成标志
  }

  async init() {
    // 自动获取主站地址（使用当前域名）
    this.mainSiteUrl = formatMainSiteUrl(window.location.origin);
    
    // 获取URL参数
    const params = getUrlParams();
    
    // 设置博客列表URL：优先使用URL参数，否则使用配置，最后使用默认值
    if (params.blog_list) {
      this.currentListUrl = params.blog_list;
    } else {
      // 尝试从配置文件加载初始博客列表URL
      try {
        // console.log('正在加载配置文件：/json/config.json');
        const res = await fetch('/json/config.json');
        if (!res.ok) {
          console.warn(`配置文件加载失败，状态码：${res.status}`);
          throw new Error(`HTTP ${res.status}`);
        }
        const config = await res.json();
        // console.log('配置文件加载成功：', config);
        
        // 设置主题色
        if (config.theme && config.theme.color) {
          document.documentElement.style.setProperty('--theme-color', config.theme.color);
          document.documentElement.style.setProperty('--theme-color-rgb', hexToRgb(config.theme.color));
        }
        

        
        // 设置初始博客列表URL
        if (config.blog && config.blog.initialListUrl) {
          this.currentListUrl = config.blog.initialListUrl;
        }
      } catch (e) {
        console.warn('加载配置文件失败，使用默认配置:', e);
        // 保持构造函数中设置的默认值：'/blogs/blogs.json'
      }
    }

    // 加载博客数据
    await this.loadBlogsData();

    // 若列表加载失败，直接显示 404 并终止
    if (this.loadingError) {
      this.show404();
      return;
    }

    // 处理 blog_id 参数
    if (params.blog_id) {
      this.redirectToBlog(params.blog_id);
      return;
    }

    // 预计算标签列表（用于 URL 序列号编解码）
    this.tagList = this.extractTags();

    // 从 URL 解析预选标签（?tag=0,1,2 序列号）
    this.parseTagParam();

    // 更新页面标题和描述
    this.updatePageTitleAndDescription();

    // 渲染搜索和标签筛选工具栏
    this.renderSearchAndFilter('blog-cards-container');

    // 渲染博客卡片（带过滤和入场动画）
    this.renderFilteredBlogCards('blog-cards-container');

    // 初始化 url 框
    this.initUrlBoxes();

    // 初始化内联复制按钮
    this.initInlineCopyButtons();

    // 添加底部一言区域
    this.addFooter();
  }

  async loadBlogsData() {
    try {
      const res = await fetch(this.currentListUrl);
      if (!res.ok) {
        throw new Error(`HTTP error ${res.status}`);
      }
      const data = await res.json();

      if (Array.isArray(data)) {
        this.currentBlogList = data;
        this.listName = 'ReOri Blog';
        this.listDescription = '';
      } else if (data && typeof data === 'object') {
        this.listName = data.name || 'ReOri Blog';
        this.listDescription = data.description || '';
        this.currentBlogList = Array.isArray(data.items) ? data.items : [];
      } else {
        this.currentBlogList = [];
        this.listDescription = '';
      }

      // 按优先级稳定排序（优先级大的在前，相同优先级保持原顺序）
    this.currentBlogList = this.stableSortByPriority(this.currentBlogList);
    this.loadingError = false; // 成功加载
    } catch (e) {
      console.error('加载博客列表失败:', e);
      this.currentBlogList = [];
      this.loadingError = true;  // 标记加载失败
    }
  }

  // 更新页面标题和描述
  updatePageTitleAndDescription() {
    const titleEl = document.getElementById('page-title');
    if (titleEl) {
      titleEl.textContent = this.listName;
    }

    // 添加或更新描述区域
    let descEl = document.getElementById('page-description');
    if (this.listDescription) {
      if (!descEl) {
        descEl = document.createElement('div');
        descEl.id = 'page-description';
        descEl.className = 'page-description';
        const container = document.querySelector('.container');
        if (container && titleEl) {
          container.insertBefore(descEl, titleEl.nextSibling);
        }
      }
      descEl.textContent = this.listDescription;
    } else if (descEl) {
      descEl.remove();
    }
  }

  // 稳定排序：根据 priority 字段降序排列，priority 默认为 0
  stableSortByPriority(array) {
    if (!Array.isArray(array) || array.length === 0) return array;

    // 为每个元素添加原始索引
    const withIndex = array.map((item, idx) => ({ item, idx }));

    // 排序：优先级大的靠前，优先级相同则索引小的靠前（即原顺序）
    withIndex.sort((a, b) => {
      const priorityA = typeof a.item.priority === 'number' ? a.item.priority : 0;
      const priorityB = typeof b.item.priority === 'number' ? b.item.priority : 0;
      if (priorityB !== priorityA) {
        return priorityB - priorityA; // 降序
      }
      return a.idx - b.idx; // 稳定原顺序
    });

    // 提取排序后的元素
    return withIndex.map(w => w.item);
  }

  redirectToBlog(blogId) {
    const blog = this.currentBlogList.find(item => item.id === blogId);
    if (blog && blog.url) {
      this.showRedirectProgress(blog.url);
    } else {
      console.warn(`未找到 ID 为 ${blogId} 的博客`);
      this.show404(); // 未找到则显示404页面
    }
  }

  // 显示重定向进度条
  showRedirectProgress(targetUrl) {
    // 解析目标网站域名用于显示
    const targetDomain = this.extractDomain(targetUrl);
    const savedTheme = localStorage.getItem('reori-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = savedTheme ? savedTheme === 'dark' : prefersDark;

    // 创建进度条容器
    const progressContainer = document.createElement('div');
    progressContainer.id = 'redirect-progress';
    progressContainer.style.cssText = `
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: ${isDark ? 'rgba(19,20,31,0.96)' : 'rgba(255,255,255,0.95)'};
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      z-index: 9999;
      font-family: Arial, sans-serif;
    `;

    // 创建进度条
    const progressBar = document.createElement('div');
    progressBar.style.cssText = `
      width: 300px;
      height: 6px;
      background: ${isDark ? '#2a2b40' : '#f0f0f0'};
      border-radius: 3px;
      overflow: hidden;
      margin-bottom: 20px;
    `;

    const progressFill = document.createElement('div');
    progressFill.style.cssText = `
      width: 0%;
      height: 100%;
      background: var(--theme-color, #5ca1ff);
      border-radius: 3px;
      transition: width 1s ease-in-out;
    `;

    // 创建主提示文字
    const text = document.createElement('div');
    text.textContent = '正在重定向';
    text.style.cssText = `
      color: ${isDark ? '#c8c8d8' : '#333'};
      font-size: 18px;
      margin-bottom: 15px;
      font-weight: bold;
    `;

    // 创建目标网站提示
    const targetInfo = document.createElement('div');
    targetInfo.textContent = `即将跳转到：${targetDomain}`;
    targetInfo.style.cssText = `
      color: ${isDark ? '#787890' : '#666'};
      font-size: 14px;
      margin-bottom: 20px;
      max-width: 300px;
      text-align: center;
      word-break: break-all;
    `;

    // 创建倒计时文字
    const countdown = document.createElement('div');
    countdown.textContent = '1秒后自动跳转';
    countdown.style.cssText = `
      color: ${isDark ? '#505068' : '#999'};
      font-size: 12px;
      margin-top: 10px;
    `;

    // 组装元素
    progressBar.appendChild(progressFill);
    progressContainer.appendChild(text);
    progressContainer.appendChild(targetInfo);
    progressContainer.appendChild(progressBar);
    progressContainer.appendChild(countdown);
    document.body.appendChild(progressContainer);

    // 开始进度条动画
    setTimeout(() => {
      progressFill.style.width = '100%';
    }, 10);

    // 950毫秒后更改倒计时文本为“正在加载”
    setTimeout(() => {
      countdown.textContent = '正在加载';
    }, 950);

    // 1秒后跳转
    setTimeout(() => {
      window.location.href = targetUrl;
    }, 1000);
  }

  // 提取域名函数
  extractDomain(url) {
    try {
      const urlObj = new URL(url);
      return urlObj.hostname;
    } catch (e) {
      return url; // 如果URL解析失败，返回原始URL
    }
  }

  show404() {
    const container = document.getElementById('blog-cards-container');
    if (!container) return;

    // 清空容器
    container.innerHTML = '';

    // 创建 404 卡片
    const errorCard = document.createElement('div');
    errorCard.className = 'blog-card-wrapper';
    errorCard.style.textAlign = 'center';
    errorCard.style.padding = '40px 20px';

    errorCard.innerHTML = `
      <div style="font-size: 72px; color: var(--theme-color, #5ca1ff); margin-bottom: 20px;">404</div>
      <div style="font-size: 24px; color: #333; margin-bottom: 10px;">页面未找到</div>
      <div style="color: #666; margin-bottom: 30px;">您访问的页面不存在或已被移除</div>
      <a href="${this.mainSiteUrl}" style="
        display: inline-block;
        background: var(--theme-color, #5ca1ff);
        color: white;
        text-decoration: none;
        padding: 10px 24px;
        border-radius: 8px;
        font-size: 16px;
        transition: background 0.2s;
      " onmouseover="this.style.background='var(--theme-color-dark, #4a90e2)'" onmouseout="this.style.background='var(--theme-color, #5ca1ff)'">返回首页</a>
    `;

    container.appendChild(errorCard);
  }

  renderBlogCards(containerId, list) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const items = list || this.currentBlogList;

    el.innerHTML = '';

    if (!items.length) {
      const emptyDiv = document.createElement('div');
      emptyDiv.style.cssText = 'text-align: center; padding: 40px 20px; color: #999; font-size: 14px;';
      emptyDiv.textContent = this.searchText || this.activeTags.length > 0 ? '未找到匹配内容' : '暂无博客';
      el.appendChild(emptyDiv);
      return;
    }

    items.forEach(item => {
      if (item.type === 'html') {
        const wrapper = document.createElement('div');
        wrapper.className = 'blog-html-fragment';
        wrapper.innerHTML = item.html;
        if (item.wrapperClass) wrapper.classList.add(...item.wrapperClass.split(' '));
        el.appendChild(wrapper);
      } else {
        const card = this.createBlogCard(item);
        el.appendChild(card);
      }
    });
  }

  createBlogCard(blog) {
    const { id, title, description, date, url } = blog;
    const cardWrapper = document.createElement('div');
    cardWrapper.className = 'blog-card-wrapper';
    if (blog.cover) cardWrapper.classList.add('has-cover');

    // 自定义主题色（未设置则使用全局默认）
    if (blog.themeColor) {
      cardWrapper.style.setProperty('--theme-color', blog.themeColor);
      cardWrapper.style.setProperty('--theme-color-rgb', hexToRgb(blog.themeColor));
    }

    const card = document.createElement('a');
    card.href = url;
    card.className = 'blog-card';

    // 封面图
    let coverHtml = '';
    if (blog.cover) {
      coverHtml = `<div class="blog-card__cover"><img src="${blog.cover}" alt="${title || '博客封面'}" loading="lazy"></div>`;
    }

    // 标签区域
    let tagsHtml = '';
    if (Array.isArray(blog.tags) && blog.tags.length) {
      tagsHtml = `<div class="blog-card__tags">${blog.tags.map(t => `<span class="blog-card__tag">${t}</span>`).join('')}</div>`;
    }

    card.innerHTML = `
      ${coverHtml}
      <div class="blog-card__content">
        <div class="blog-card__title">${title || '无标题'}</div>
        <div class="blog-card__desc">${description || '无描述'}</div>
        <div class="blog-card__date">${date || '未知日期'}</div>
      </div>
      ${tagsHtml}
      <div class="blog-card__actions">
        <button class="blog-card__copy-btn" title="复制地址">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: block;"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        </button>
        <button class="blog-card__btn">阅读</button>
      </div>
    `;

    const copyBtn = card.querySelector('.blog-card__copy-btn');
    copyBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();

      let copyUrl = url;
      if (id && this.currentListUrl) {
        const listPath = this.currentListUrl.startsWith('/')
          ? this.currentListUrl.slice(1)
          : this.currentListUrl;
        copyUrl = `${this.mainSiteUrl}?blog_list=${listPath}&blog_id=${id}`;
      }

      this.copyToClipboard(copyUrl);
    });

    cardWrapper.appendChild(card);
    return cardWrapper;
  }

  // 初始化 url 框（原 mini 链接）
  initUrlBoxes() {
    const elements = document.querySelectorAll('[data-url-title]');
    elements.forEach(el => {
      const title = el.dataset.urlTitle || '未知链接';
      const url = el.dataset.url || '#';

      const link = document.createElement('a');
      link.href = url;
      link.className = 'url-card';
      link.innerHTML = `
        <img src="/images/url.svg" alt="链接" class="url-card__icon">
        <span class="url-card__text">${title}</span>
      `;
      el.innerHTML = '';
      el.appendChild(link);
    });
  }

  // 初始化内联复制按钮（带文字）
  initInlineCopyButtons() {
    const elements = document.querySelectorAll('[data-copy]');
    elements.forEach(el => {
      const copyText = el.dataset.copy;
      if (!copyText) return;

      const button = document.createElement('button');
      button.className = 'inline-copy-btn';
      button.title = '复制内容';
      button.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: inline-block; vertical-align: middle; height: 1em; width: auto;"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>${el.textContent}`;

      button.addEventListener('click', (e) => {
        e.stopPropagation();
        this.copyToClipboard(copyText);
      });

      el.replaceWith(button);
    });
  }

  // ===== 搜索 + 多选标签筛选 + 入场动画 =====

  // 从 URL 解析预选标签（序列号 → 标签名）
  parseTagParam() {
    const params = getUrlParams();
    if (params.tag && this.tagList && this.tagList.length) {
      const indices = params.tag.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
      this.activeTags = indices.map(i => this.tagList[i]).filter(Boolean);
    }
    this.filterMode = params.filter_mode === 'and' ? 'and' : 'or';
  }

  // 同步当前筛选状态到 URL（标签名 → 序列号）
  syncURL() {
    const params = getUrlParams();
    const parts = [];
    if (params.blog_list) parts.push('blog_list=' + params.blog_list);
    if (this.activeTags.length > 0 && this.tagList) {
      const indices = this.activeTags.map(t => this.tagList.indexOf(t)).filter(i => i >= 0);
      parts.push('tag=' + indices.join(','));
      if (this.filterMode === 'and') parts.push('filter_mode=and');
    }
    const search = parts.join('&');
    const newURL = search ? `${window.location.pathname}?${search}` : window.location.pathname;
    history.replaceState(null, '', newURL);
  }

  // 渲染搜索框和多选标签筛选栏
  renderSearchAndFilter(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    if (document.getElementById('blog-toolbar')) return;

    const toolbar = document.createElement('div');
    toolbar.id = 'blog-toolbar';
    toolbar.className = 'blog-toolbar';

    // 搜索输入框
    const searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'blog-search';
    searchInput.placeholder = '搜索博客...';
    searchInput.addEventListener('input', (e) => {
      this.searchText = e.target.value;
      this.renderFilteredBlogCards(containerId);
    });

    // 标签筛选栏
    const filterBar = document.createElement('div');
    filterBar.className = 'filter-bar';

    // "全部" 按钮（与其他标签互斥）
    const allChip = document.createElement('button');
    allChip.className = 'filter-chip' + (this.activeTags.length === 0 ? ' active' : '');
    allChip.textContent = '全部';
    allChip.addEventListener('click', () => {
      this.activeTags = [];
      filterBar.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
      allChip.classList.add('active');
      this.hideFilterMode();
      this.syncURL();
      this.renderFilteredBlogCards(containerId);
    });
    filterBar.appendChild(allChip);

    // 各个标签按钮（多选 toggle）
    const tags = this.extractTags();
    tags.forEach(tag => {
      const chip = document.createElement('button');
      chip.className = 'filter-chip' + (this.activeTags.includes(tag) ? ' active' : '');
      chip.textContent = tag;
      chip.dataset.tag = tag;
      chip.addEventListener('click', () => {
        const idx = this.activeTags.indexOf(tag);
        if (idx > -1) {
          this.activeTags.splice(idx, 1);
          chip.classList.remove('active');
        } else {
          this.activeTags.push(tag);
          chip.classList.add('active');
        }

        // 更新"全部"状态
        const allActive = this.activeTags.length === 0;
        allChip.classList.toggle('active', allActive);

        // 显示/隐藏筛选模式切换
        if (this.activeTags.length >= 2) {
          this.showFilterMode(containerId);
        } else {
          this.hideFilterMode();
        }

        this.syncURL();
        this.renderFilteredBlogCards(containerId);
      });
      filterBar.appendChild(chip);
    });

    toolbar.appendChild(searchInput);
    if (filterBar.children.length > 1) toolbar.appendChild(filterBar);

    // 筛选模式切换（且/或），初始隐藏
    const modeContainer = document.createElement('div');
    modeContainer.id = 'filter-mode';
    modeContainer.className = 'filter-mode';
    if (this.activeTags.length < 2) modeContainer.style.display = 'none';

    const modeLabel = document.createElement('span');
    modeLabel.className = 'filter-mode-label';
    modeLabel.textContent = '标签筛选:';

    const andBtn = document.createElement('button');
    andBtn.className = 'filter-mode-btn' + (this.filterMode === 'and' ? ' active' : '');
    andBtn.dataset.mode = 'and';
    andBtn.textContent = '全部匹配';
    andBtn.addEventListener('click', () => {
      if (this.filterMode !== 'and') {
        this.filterMode = 'and';
        modeContainer.querySelectorAll('.filter-mode-btn').forEach(b => b.classList.remove('active'));
        andBtn.classList.add('active');
        this.syncURL();
        this.renderFilteredBlogCards(containerId);
      }
    });

    const orBtn = document.createElement('button');
    orBtn.className = 'filter-mode-btn' + (this.filterMode === 'or' ? ' active' : '');
    orBtn.dataset.mode = 'or';
    orBtn.textContent = '任意匹配';
    orBtn.addEventListener('click', () => {
      if (this.filterMode !== 'or') {
        this.filterMode = 'or';
        modeContainer.querySelectorAll('.filter-mode-btn').forEach(b => b.classList.remove('active'));
        orBtn.classList.add('active');
        this.syncURL();
        this.renderFilteredBlogCards(containerId);
      }
    });

    modeContainer.append(modeLabel, andBtn, orBtn);
    toolbar.appendChild(modeContainer);

    // 插入到页面标题/描述之后
    const descEl = document.getElementById('page-description');
    const titleEl = document.getElementById('page-title');
    const refEl = descEl || titleEl;
    if (refEl && refEl.parentNode) {
      refEl.parentNode.insertBefore(toolbar, refEl.nextSibling);
    } else {
      container.parentNode.insertBefore(toolbar, container);
    }
  }

  showFilterMode() {
    const el = document.getElementById('filter-mode');
    if (el) el.style.display = 'flex';
  }

  hideFilterMode() {
    const el = document.getElementById('filter-mode');
    if (el) el.style.display = 'none';
  }

  // 从当前博客数据提取所有标签（跳过 HTML 片段）
  extractTags() {
    const tagSet = new Set();
    this.currentBlogList.forEach(item => {
      if (item.type === 'html') return;
      if (Array.isArray(item.tags)) {
        item.tags.forEach(t => tagSet.add(t));
      }
    });
    return Array.from(tagSet);
  }

  // 获取当前过滤后的博客列表（HTML 片段始终保留在原位）
  getFilteredList() {
    const htmlFragments = this.currentBlogList.reduce((acc, item, idx) => {
      if (item.type === 'html') acc.push(idx);
      return acc;
    }, []);

    // 只对非 HTML 片段进行搜索和标签过滤
    let filtered = this.currentBlogList.filter(item => item.type !== 'html');

    if (this.searchText) {
      const q = this.searchText.toLowerCase().trim();
      if (q) {
        filtered = filtered.filter(item =>
          (item.title || '').toLowerCase().includes(q) ||
          (item.description || '').toLowerCase().includes(q)
        );
      }
    }

    if (this.activeTags.length > 0) {
      if (this.filterMode === 'and') {
        filtered = filtered.filter(item =>
          this.activeTags.every(tag =>
            Array.isArray(item.tags) && item.tags.includes(tag)
          )
        );
      } else {
        filtered = filtered.filter(item =>
          this.activeTags.some(tag =>
            Array.isArray(item.tags) && item.tags.includes(tag)
          )
        );
      }
    }

    // 将 HTML 片段按原位置插回
    const result = [];
    let filteredIdx = 0;
    for (let i = 0; i < this.currentBlogList.length; i++) {
      if (htmlFragments.includes(i)) {
        result.push(this.currentBlogList[i]);
      } else {
        if (filteredIdx < filtered.length &&
            this.currentBlogList[i] === filtered[filteredIdx]) {
          result.push(filtered[filteredIdx]);
          filteredIdx++;
        }
      }
    }
    // 追加剩余的过滤结果（理论上不会发生，但安全处理）
    while (filteredIdx < filtered.length) {
      result.push(filtered[filteredIdx++]);
    }

    return result;
  }

  // 渲染过滤后的卡片并触发入场动画
  renderFilteredBlogCards(containerId) {
    const filtered = this.getFilteredList();
    this.renderBlogCards(containerId, filtered);

    if (!this.hasInitialRender) {
      this.setupEntranceAnimation();
      this.hasInitialRender = true;
    } else {
      document.querySelectorAll('.blog-card-wrapper, .blog-html-fragment').forEach(w => w.classList.add('visible'));
    }
  }

  // 卡片入场动画：链式加载，每张卡自己等40ms后加上visible
  setupEntranceAnimation() {
    const wrappers = document.querySelectorAll('.blog-card-wrapper, .blog-html-fragment');
    if (!wrappers.length) return;

    if (!('IntersectionObserver' in window)) {
      wrappers.forEach(w => w.classList.add('visible'));
      return;
    }

    const scheduled = new Set();

    const reveal = (el) => {
      if (scheduled.has(el)) return;
      scheduled.add(el);
      setTimeout(() => {
        el.classList.add('visible');
        const idx = Array.from(wrappers).indexOf(el);
        tryReveal(idx + 1);
      }, 40);
    };

    const tryReveal = (fromIdx) => {
      for (let i = fromIdx; i < wrappers.length; i++) {
        const el = wrappers[i];
        if (scheduled.has(el) || el.classList.contains('visible')) continue;

        const rect = el.getBoundingClientRect();
        // 已滚过视口上方，直接标为可见
        if (rect.bottom < 0) {
          el.classList.add('visible');
          continue;
        }
        // 还没进入视口
        if (rect.top >= window.innerHeight) return;

        // 在视口中
        if (i === 0) { reveal(el); return; }

        const prev = wrappers[i - 1];
        if (prev.classList.contains('visible') || prev.getBoundingClientRect().bottom < 0) {
          reveal(el);
          return;
        }
        return;
      }
    };

    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          tryReveal(0);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0 });

    wrappers.forEach(w => observer.observe(w));
    tryReveal(0);
  }

  // 添加底部一言区域（不包含刷新按钮）
  addFooter() {
    if (document.querySelector('.footer')) return;

    const footer = document.createElement('footer');
    footer.className = 'footer';

    const line = document.createElement('div');
    line.className = 'footer__line';
    line.textContent = '----再怎么找也没有了----';

    const sentenceDiv = document.createElement('div');
    sentenceDiv.className = 'footer__sentence';
    sentenceDiv.textContent = '加载一言中...';

    const refreshBtn = document.createElement('button');
    refreshBtn.className = 'footer__refresh';
    refreshBtn.title = '刷新一言';
    refreshBtn.innerHTML = `
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="23 4 23 10 17 10"/>
        <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
      </svg>
    `;

    const sentenceRow = document.createElement('div');
    sentenceRow.className = 'footer__sentence-row';
    sentenceRow.appendChild(sentenceDiv);

    footer.appendChild(line);
    footer.appendChild(sentenceRow);
    footer.appendChild(refreshBtn);
    document.body.appendChild(footer);

    const loadSentence = () => {
      sentenceDiv.textContent = '加载一言中...';
      fetch('/json/sentence.json')
        .then(response => response.json())
        .then(data => {
          const randomIndex = Math.floor(Math.random() * data.length);
          const item = data[randomIndex];
          const sentence = item.sentence;
          const from = item.from;
          const displayText = `「${sentence}」\n——${from}`;
          sentenceDiv.textContent = displayText;
          this.currentSentenceText = `「${sentence}」——${from}`;
        })
        .catch(error => {
          console.error('加载一言失败:', error);
          sentenceDiv.textContent = '一言加载失败';
        });
    };

    loadSentence();

    refreshBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      refreshBtn.classList.add('spinning');
      loadSentence();
    });

    sentenceDiv.addEventListener('click', () => {
      this.copyToClipboard(this.currentSentenceText);
    });

    // 旋转动画结束后移除类
    refreshBtn.addEventListener('animationend', () => {
      refreshBtn.classList.remove('spinning');
    });

    // 底部 Overscroll 动画：尝试继续下滑时页脚颜色渐变 + 句子放大
    let pullAccum = 0;
    const MAX_PULL = 120;
    let resetTimer;

    const animateFooter = (progress) => {
      const r = 57, g = 159, b = 255;
      // 上边界升起感：边框加粗 + 向上扩散的阴影
      footer.style.borderTopWidth = `${1 + progress * 1.5}px`;
      footer.style.borderTopColor = `rgba(${r},${g},${b},${0.2 + progress * 0.5})`;
      footer.style.boxShadow = `0 -${progress * 10}px ${progress * 16}px -2px rgba(${r},${g},${b},${progress * 0.15})`;
      // 背景：半透明白 → 主题色渐显
      footer.style.background = `rgba(${r},${g},${b},${0.08 + progress * 0.25})`;
      // backdrop-filter 强度随下拉增加
      footer.style.backdropFilter = `blur(${4 + progress * 8}px)`;
      // 句子略微放大
      sentenceDiv.style.transform = `scale(${1 + progress * 0.08})`;
      sentenceDiv.style.color = progress > 0.5
        ? `rgba(${Math.min(255, r + (255-r)*(progress-0.5)*2)}, ${Math.min(255, g + (255-g)*(progress-0.5)*2)}, 255, 1)`
        : '';
      // "再找" 行：字间距拉开 + 渐隐 + 变色加粗
      line.style.letterSpacing = `${progress * 3}px`;
      line.style.opacity = 1 - progress * 0.5;
      line.style.color = `rgba(${r},${g},${b},${0.3 + progress * 0.7})`;
      line.style.fontWeight = progress > 0.3 ? '700' : '';
    };

    const resetFooter = () => {
      if (pullAccum === 0) return;
      pullAccum = 0;
      const trans = '0.5s cubic-bezier(0.2, 0.9, 0.3, 1)';
      footer.style.transition = `background ${trans}, border-top-width ${trans}, border-top-color ${trans}, box-shadow ${trans}, backdrop-filter ${trans}`;
      sentenceDiv.style.transition = `transform ${trans}, color ${trans}`;
      line.style.transition = `letter-spacing ${trans}, opacity ${trans}, color ${trans}, font-weight ${trans}`;
      footer.style.background = '';
      footer.style.borderTopWidth = '';
      footer.style.borderTopColor = '';
      footer.style.boxShadow = '';
      footer.style.backdropFilter = '';
      sentenceDiv.style.transform = '';
      sentenceDiv.style.color = '';
      line.style.letterSpacing = '';
      line.style.color = '';
      line.style.fontWeight = '';
      line.style.opacity = '';
      setTimeout(() => {
        footer.style.transition = '';
        sentenceDiv.style.transition = '';
        line.style.transition = '';
      }, 500);
    };

    // 顶部 Overscroll：在页面最顶上继续上滑时内容被轻微拉下
    let topPull = 0;
    const MAX_TOP_PULL = 80;
    const container = document.querySelector('.container');
    let topResetTimer;

    const resetTop = () => {
      if (topPull === 0) return;
      topPull = 0;
      const trans = '0.5s cubic-bezier(0.2, 0.9, 0.3, 1)';
      container.style.transition = `transform ${trans}`;
      container.style.transform = '';
      setTimeout(() => { container.style.transition = ''; }, 500);
    };

    window.addEventListener('wheel', (e) => {
      const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      const atTop = window.scrollY <= 0;

      // 顶部 overscroll：上滑（deltaY < 0）
      if (atTop && e.deltaY < 0 && topPull < MAX_TOP_PULL) {
        topPull = Math.min(topPull + Math.abs(e.deltaY) * 0.3, MAX_TOP_PULL);
        container.style.transform = `translateY(${topPull * 0.3}px)`;
      }
      if (topPull > 0 && (!atTop || e.deltaY > 0)) {
        resetTop();
      }

      // 底部 overscroll（页脚动画）
      if (atBottom && e.deltaY > 0 && pullAccum < MAX_PULL) {
        pullAccum = Math.min(pullAccum + e.deltaY * 0.35, MAX_PULL);
        animateFooter(pullAccum / MAX_PULL);
      }
      if (pullAccum > 0 && (!atBottom || e.deltaY < 0)) {
        resetFooter();
      }

      clearTimeout(resetTimer);
      if (pullAccum > 0) resetTimer = setTimeout(resetFooter, 200);
      clearTimeout(topResetTimer);
      if (topPull > 0) topResetTimer = setTimeout(resetTop, 200);
    }, { passive: true });
  }

  copyToClipboard(text) {
    let toast = document.querySelector('.copy-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.className = 'copy-toast';
      toast.innerHTML = '<span class="toast-address"></span><span>复制成功</span>';
      document.body.appendChild(toast);
    }
    const addressSpan = toast.querySelector('.toast-address');
    if (addressSpan) {
      addressSpan.textContent = text + ' ';
    } else {
      toast.textContent = text + ' 复制成功';
    }

    navigator.clipboard.writeText(text).then(() => {
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2000);
    }).catch(err => {
      console.error('复制失败:', err);
      if (addressSpan) {
        addressSpan.textContent = '复制失败！';
      } else {
        toast.textContent = '复制失败！';
      }
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2000);
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const blogRenderer = new BlogCardRenderer();
  blogRenderer.init();
  window.blogRenderer = blogRenderer;
});

// ===== AutoUpdate Timestamp (do not remove) =====
// 此区块由 AutoUpdate Server 自动维护
(function() {
  console.log(
    '%c📦 AutoUpdate',
    'color: #4CAF50; font-size: 13px; font-weight: bold;'
  );
  console.log(
    '  最新版本更新时间 (GitHub): %s',
    '未知'
  );
  console.log(
    '  最新检查时间: %s',
    '2026.06.02 21:17:57 [UTC+8]'
  );
})();
// ===== End AutoUpdate Timestamp =====
