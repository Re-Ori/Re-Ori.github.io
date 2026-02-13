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

// 博客渲染类
class BlogCardRenderer {
  constructor() {
    this.mainSiteUrl = "http://127.0.0.1:5500/";
    this.currentBlogList = [];
    this.currentListUrl = '/data/blogs.json'; // 默认列表
  }

  // 初始化：加载配置 + 处理 URL 参数
  async init() {
    // 加载主站地址配置
    try {
      const res = await fetch('/data/navbar.json');
      const config = await res.json();
      if (config.mainSiteUrl) {
        this.mainSiteUrl = formatMainSiteUrl(config.mainSiteUrl);
      }
    } catch (e) {
      console.warn('加载主站地址失败，使用默认值:', e);
    }

    // 解析 URL 参数
    const params = getUrlParams();

    // 1. 处理 blog_list 参数：加载指定 JSON
    if (params.blog_list) {
      this.currentListUrl = params.blog_list;
    }

    // 2. 加载博客列表
    await this.loadBlogsData();

    // 3. 处理 blog_id 参数：重定向到对应博客，若找不到则显示404
    if (params.blog_id) {
      this.redirectToBlog(params.blog_id);
      return; // 无论是否找到，此处返回，避免后续渲染卡片
    }

    // 4. 渲染博客列表（主页）
    if (document.getElementById('blog-cards-container')) {
      this.renderBlogCards('blog-cards-container');
    }

    // 5. 初始化小链接
    this.initMiniBlogCards();
  }

  // 加载博客数据（支持指定 JSON）
  async loadBlogsData() {
    try {
      const res = await fetch(this.currentListUrl);
      const data = await res.json();
      this.currentBlogList = Array.isArray(data) ? data : [];
      return this.currentBlogList;
    } catch (e) {
      console.error('加载博客列表失败:', e);
      this.currentBlogList = [];
      return [];
    }
  }

  // 根据 blog_id 重定向或显示404
  redirectToBlog(blogId) {
    const blog = this.currentBlogList.find(item => item.id === blogId);
    if (blog && blog.url) {
      window.location.href = blog.url; // 找到则跳转
    } else {
      console.warn(`未找到 ID 为 ${blogId} 的博客`);
      this.show404(); // 未找到则显示404页面
    }
  }

  // 显示 404 错误页面（包含返回首页按钮）
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
      <div style="font-size: 72px; color: #5ca1ff; margin-bottom: 20px;">404</div>
      <div style="font-size: 24px; color: #333; margin-bottom: 10px;">博客未找到</div>
      <div style="color: #666; margin-bottom: 30px;">您访问的博客 ID 不存在或已被移除</div>
      <a href="${this.mainSiteUrl}" style="
        display: inline-block;
        background: #5ca1ff;
        color: white;
        text-decoration: none;
        padding: 10px 24px;
        border-radius: 8px;
        font-size: 16px;
        transition: background 0.2s;
      " onmouseover="this.style.background='#4a90e2'" onmouseout="this.style.background='#5ca1ff'">返回首页</a>
    `;

    container.appendChild(errorCard);
  }

  // 渲染博客卡片（原格式，带复制按钮）
  renderBlogCards(containerId) {
    const el = document.getElementById(containerId);
    if (!el || !this.currentBlogList.length) return;

    el.innerHTML = '';
    this.currentBlogList.forEach(blog => {
      const card = this.createBlogCard(blog);
      el.appendChild(card);
    });
  }

  // 创建博客卡片（修正按钮布局：copy在阅读左侧，按钮组在右下角）
  createBlogCard(blog) {
    const { id, title, description, date, url } = blog;
    const cardWrapper = document.createElement('div');
    cardWrapper.className = 'blog-card-wrapper';

    // 博客卡片（改为列布局，按钮组在右下角）
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
        <!-- Copy 在阅读左侧 -->
        <button class="blog-card__copy-btn" title="复制地址">
          <img src="/images/copy.svg" alt="复制" style="height: 16px; width: auto;">
        </button>
        <button class="blog-card__btn">阅读</button>
      </div>
    `;

    // 复制按钮逻辑
    const copyBtn = card.querySelector('.blog-card__copy-btn');
    copyBtn.addEventListener('click', (e) => {
      e.preventDefault(); // 阻止跳转
      e.stopPropagation();

      let copyUrl = url;
      // JSON 来源博客：生成带参数的地址
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

  // 初始化小链接
  initMiniBlogCards() {
    const miniLinkElements = document.querySelectorAll('[data-blog-mini-title]');
    miniLinkElements.forEach(el => {
      const title = el.dataset.blogMiniTitle || '未知链接';
      const url = el.dataset.blogMiniUrl || '#';

      const miniLink = document.createElement('a');
      miniLink.href = url;
      miniLink.className = 'blog-card-mini';
      miniLink.textContent = `→ ${title}`;
      el.appendChild(miniLink);
    });
  }

  // 复制到剪贴板（使用 navbar.js 创建的 toast 结构）
  copyToClipboard(text) {
    // 查找已有的 toast 元素（由 navbar.js 创建）
    let toast = document.querySelector('.copy-toast');
    if (!toast) {
      // 如果不存在（极少数情况），按相同结构创建
      toast = document.createElement('div');
      toast.className = 'copy-toast';
      toast.innerHTML = '<span class="toast-address"></span><span>复制成功</span>';
      document.body.appendChild(toast);
    }
    const addressSpan = toast.querySelector('.toast-address');
    if (addressSpan) {
      addressSpan.textContent = text + ' ';
    } else {
      // 兼容旧结构（理论上不会发生）
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

// 页面加载后初始化
document.addEventListener('DOMContentLoaded', () => {
  const blogRenderer = new BlogCardRenderer();
  blogRenderer.init();
  window.blogRenderer = blogRenderer;
});