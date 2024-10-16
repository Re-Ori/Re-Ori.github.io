document.addEventListener('DOMContentLoaded', () => {
    const urlParams = new URLSearchParams(window.location.search);
    const blogId = urlParams.get('id');

    if (urlParams.get('json')) {
        var json_url = urlParams.get('json')
        const back = document.getElementById('back');
        back.href = `index.html?json=${json_url}`;
    } else {
        var json_url = 'json/blog.json';
    }

    fetch(json_url)
        .then(response => response.json())
        .then(data => {
            if (blogId.slice(0, 7) == "[COUNT]") {
                // 当输入id为"[COUNT]+数字x"时,则跳转到此blog集的第x项
                var blog = data.blogs[Number(blogId.slice(7,))];
            } else {
                var blog = data.blogs.find(b => b.id === blogId);
            }
            const blogDetail = document.getElementById('blog-detail');

            if (blog) {
                document.title = `blog详情-${blog.title}`;

                if ("special" in blog) {
                    if ("password" in blog.special) {
                        var password = prompt("此条BLOG需要秘钥：");
                        if (password != null) {
                            if (password != blog.special.password) {
                                alert("秘钥错误,点击返回");
                                window.location.href = "index.html";//输入密码错误时重定向
                            }
                        } else {
                            window.location.href = "index.html";//取消输入时重定向
                        }
                    }
                }

                const converter = new showdown.Converter()
                blogDetail.innerHTML = `
                    <h2>${blog.title}</h2>
                    <p style="color:#ddd;">${blog.date}</p>
                `;

                if ("mdcontent" in blog) {
                    console.log(blog.mdcontent);
                    fetch(blog.mdcontent)
                        .then(response => response.text())
                        .then(md => {
                            const htmlMd = converter.makeHtml(md);
                            blogDetail.innerHTML = `${htmlMd}`;
                        });
                } else {
                    blog.content.forEach(line => {
                        const htmlLine = converter.makeHtml(line);
                        blogDetail.innerHTML += `\n${htmlLine}`;
                    });
                }

            } else {
                blogDetail.innerHTML = '<h1>404 NOT FOUND</h1><p>BLOG内容未找到。</p><a href="index.html">返回主页</a></span>';
            }
        })
        .catch(error => {
            console.error('Error loading blog data:', error);
            document.getElementById('blog-detail').innerHTML = `<p>无法加载BLOG内容。[${error}]</p><br><span>尝试<a href="index.html">返回主页</a>或看看控制台有没有报错?</span>`;
        });
});