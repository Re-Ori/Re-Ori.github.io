// 添加时间更新函数
const updateTimeTag = () => {
    const timeTag = document.getElementById('tag-Time');
    if (timeTag) {
        const now = new Date();
        const timeString = now.toLocaleTimeString();
        timeTag.textContent = timeString;
    }
};
// 每秒更新一次时间标签
setInterval(updateTimeTag, 100);

// 搜索
const search = () => {
    const searchInput = prompt('请输入要搜索的内容：');
    if (searchInput) {
        const searchQuery = encodeURIComponent(searchInput);
        const bingUrl = `https://www.bing.com/search?q=${searchQuery}`;
        window.location.href = bingUrl;
    }
}

// 计算距离 2025 年 6 月 21 日的天数
const updateDaysTo20250621 = () => {
    const targetDate = new Date('2025-06-21');
    const now = new Date();
    const diffTime = Math.abs(targetDate - now);
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
    
    const daysTag = document.getElementById('tag-DaysTo20250621');
    if (daysTag) {
        daysTag.textContent = `距离中考仅有 ${diffDays} 天`;
    }
};

// 每秒更新一次天数标签
setInterval(updateDaysTo20250621, 1000);