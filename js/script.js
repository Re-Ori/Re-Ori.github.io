// 读取config.json文件,给导航栏(document.querySelector("body > div > div.header"))添加链接
fetch('config.json')
    .then(response => response.json())
    .then(data => {
        const nav = document.querySelector("body > div > div.header");
        console.log(data.nav);
        data.nav.forEach(item => {
            const link = document.createElement("a");
            link.href = item.url;
            link.textContent = item.name;
            link.classList.add("a-no-style");

            // 去除下划线,从左到右排列在右侧,上下居中,不换行
            link.style.textDecoration = "none";
            link.style.padding = "0 10px";
            link.style.fontSize = "16px";
            link.style.lineHeight = "40px";
            link.style.verticalAlign = "middle";
            link.style.whiteSpace = "nowrap";
            // 鼠标悬停时添加下划线
            link.addEventListener("mouseover", () => {
                link.style.textDecoration = "underline";
            });
            // 鼠标离开时移除下划线
            link.addEventListener("mouseout", () => {
                link.style.textDecoration = "none";
            });

            nav.appendChild(link);
        });
    })


// 读取config.json文件,将document.querySelector("body > div > span > a:nth-child(1)")替换为config.json中的version值
fetch('config.json')
    .then(response => response.json())
    .then(data => {
        console.log(`%c Re-Ori %c ${data.version} %c`, "background:#35495e ; padding: 1px; border-radius: 3px 0 0 3px;  color: #fff", "background:#41b883 ; padding: 1px; border-radius: 0 3px 3px 0;  color: #fff", "background:transparent")
        document.querySelector("body > div > span > a:nth-child(1)").textContent = data.version;
    })
    .catch(error => console.error('Error loading the JSON file:', error));

// 每0.2s将所有未加载完成的图片添加模糊效果和灰白条纹背景,加载完成的移除效果,持续100次
var count = 100;
setInterval(() => {
    const images = document.querySelectorAll('img');
    images.forEach(image => {
        if (!image.complete) {
            image.style.filter = 'blur(5px)';
            image.style.backgroundImage = 'repeating-linear-gradient(-45deg, #00000044 0px, #00000044 20px, #00000000 20px, #00000000 40px)';
        } else {
            image.style.filter = 'blur(0px)';
            image.style.backgroundImage = 'none';
        }
    });
    count--;
    if (count == 0) {
        clearInterval();
    }
}, 200);

// 一言
function loadSentence() {
    fetch('json/sentence.json')
        .then(response => response.json())
        .then(data => {
            const randomIndex = Math.floor(Math.random() * data.length);
            const sentence = data[randomIndex].sentence;
            const sentence_from = data[randomIndex].from;
            const sentence_id = data[randomIndex].id;
            document.getElementById('sentence').style.whiteSpace = 'pre-line';
            document.getElementById('sentence').textContent = `「${sentence}」\n——${sentence_from}\n[ID ${sentence_id}]`;

            // 点击一言复制到剪切板
            document.getElementById('sentence').addEventListener('click', () => {
                const textarea = document.createElement('textarea');
                textarea.value = `「${sentence}」\n——${sentence_from}\n[ID ${sentence_id}]`;
                navigator.clipboard.writeText(`「${sentence}」——${sentence_from}`)
                document.getElementById('sentence').textContent = `「${sentence}」\n——${sentence_from}\n[已复制]`;
                document.getElementById('sentence').style.color = "#16c000";
                setTimeout(() => document.getElementById('sentence').textContent = `「${sentence}」\n——${sentence_from}\n[ID ${sentence_id}]`, 500)
                setTimeout(() => document.getElementById('sentence').style.color = "", 500)
            });


        })
        .catch(error => console.error('Error loading the JSON file:', error));
}


document.getElementById('refresh-btn').addEventListener('click', () => {
    loadSentence();
});

loadSentence();