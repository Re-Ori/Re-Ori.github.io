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
    this.mainSiteUrl = "http://127.0.0.1:5500/";
    this.currentBlogList = [];
    this.currentListUrl = '/data/blogs.json';
    this.listName = 'ReOri Blog';
    this.currentSentenceText = '';   // 存储当前一言文本，用于复制
  }

  async init() {
    // 加载主站地址和主题色配置
    try {
      const res = await fetch('/data/navbar.json');
      const config = await res.json();
      if (config.mainSiteUrl) {
        this.mainSiteUrl = formatMainSiteUrl(config.mainSiteUrl);
      }
      if (config.themeColor) {
        document.documentElement.style.setProperty('--theme-color', config.themeColor);
        document.documentElement.style.setProperty('--theme-color-rgb', hexToRgb(config.themeColor));
      }
    } catch (e) {
      console.warn('加载主站地址失败，使用默认值:', e);
    }

    const params = getUrlParams();

    if (params.blog_list) {
      this.currentListUrl = params.blog_list;
    }

    await this.loadBlogsData();

    if (params.blog_id) {
      this.redirectToBlog(params.blog_id);
      return;
    }

    const titleEl = document.getElementById('page-title');
    if (titleEl) titleEl.textContent = this.listName;

    if (document.getElementById('blog-cards-container')) {
      this.renderBlogCards('blog-cards-container');
    }

    this.initUrlBoxes();
    this.initInlineCopyButtons();
    this.addFooter(); // 添加底部一言区域
  }

  async loadBlogsData() {
    try {
      const res = await fetch(this.currentListUrl);
      const data = await res.json();

      if (Array.isArray(data)) {
        this.currentBlogList = data;
        this.listName = 'ReOri Blog';
      } else if (data && typeof data === 'object') {
        this.listName = data.name || 'ReOri Blog';
        this.currentBlogList = Array.isArray(data.items) ? data.items : [];
      } else {
        this.currentBlogList = [];
      }
    } catch (e) {
      console.error('加载博客列表失败:', e);
      this.currentBlogList = [];
    }
  }

  redirectToBlog(blogId) {
    const blog = this.currentBlogList.find(item => item.id === blogId);
    if (blog && blog.url) {
      window.location.href = blog.url;
    } else {
      console.warn(`未找到 ID 为 ${blogId} 的博客`);
      this.show404();
    }
  }

  show404() {
    const container = document.getElementById('blog-cards-container');
    if (!container) return;
    container.innerHTML = '';
    const errorCard = document.createElement('div');
    errorCard.className = 'blog-card-wrapper';
    errorCard.style.textAlign = 'center';
    errorCard.style.padding = '40px 20px';
    errorCard.innerHTML = `
      <div style="font-size: 72px; color: var(--theme-color, #5ca1ff); margin-bottom: 20px;">404</div>
      <div style="font-size: 24px; color: #333; margin-bottom: 10px;">博客未找到</div>
      <div style="color: #666; margin-bottom: 30px;">您访问的博客 ID 不存在或已被移除</div>
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

  renderBlogCards(containerId) {
    const el = document.getElementById(containerId);
    if (!el || !this.currentBlogList.length) return;

    el.innerHTML = '';
    this.currentBlogList.forEach(blog => {
      const card = this.createBlogCard(blog);
      el.appendChild(card);
    });
  }

  createBlogCard(blog) {
    const { id, title, description, date, url } = blog;
    const cardWrapper = document.createElement('div');
    cardWrapper.className = 'blog-card-wrapper';

    const card = document.createElement('a');
    card.href = url;
    card.className = 'blog-card';
    card.innerHTML = `
      <div class="blog-card__content">
        <div class="blog-card__title">${title || '无标题'}</div>
        <div class="blog-card__desc">${description || '无描述'}</div>
        <div class="blog-card__date">${date || '未知日期'}</div>
      </div>
      <div class="blog-card__actions">
        <button class="blog-card__copy-btn" title="复制地址">
          <img src="/images/copy.svg" alt="复制" style="height: 16px; width: auto;">
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
      button.innerHTML = `<img src="/images/copy.svg" alt="复制">${el.textContent}`;

      button.addEventListener('click', (e) => {
        e.stopPropagation();
        this.copyToClipboard(copyText);
      });

      el.replaceWith(button);
    });
  }

  // 添加底部一言区域（不包含刷新按钮）
  addFooter() {
    // 避免重复添加
    if (document.querySelector('.footer')) return;

    const footer = document.createElement('footer');
    footer.className = 'footer';

    const line = document.createElement('div');
    line.className = 'footer__line';
    line.textContent = '- 再怎么找也没有啦 -';

    const sentenceDiv = document.createElement('div');
    sentenceDiv.className = 'footer__sentence';
    sentenceDiv.textContent = '加载一言中...';

    footer.appendChild(line);
    footer.appendChild(sentenceDiv);
    document.body.appendChild(footer);

    // 加载一言
    fetch('/data/sentence.json')
      .then(response => response.json())
      .then(data => {
        const randomIndex = Math.floor(Math.random() * data.length);
        const item = data[randomIndex];
        const sentence = item.sentence;
        const from = item.from;
        const displayText = `「${sentence}」\n——${from}`;
        sentenceDiv.textContent = displayText;
        this.currentSentenceText = `「${sentence}」——${from}`; // 用于复制

        // 点击复制一言
        sentenceDiv.addEventListener('click', () => {
          this.copyToClipboard(this.currentSentenceText);
        });
      })
      .catch(error => {
        console.error('加载一言失败:', error);
        sentenceDiv.textContent = '一言加载失败';
      });
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