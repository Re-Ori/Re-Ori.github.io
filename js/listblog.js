document.addEventListener('DOMContentLoaded', () => {
    console.log("%c Re-Ori %c 2024 %c", "background:#35495e ; padding: 1px; border-radius: 3px 0 0 3px;  color: #fff", "background:#41b883 ; padding: 1px; border-radius: 0 3px 3px 0;  color: #fff", "background:transparent")
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('json')) {
        var json_url = urlParams.get('json');
        const leftPanel = document.getElementsByClassName('left-panel')[0];
        console.log(leftPanel);
        const back = document.createElement('a');
        back.id = "back";
        back.innerText = "返回";
        back.href = "index.html"
        leftPanel.appendChild(back);
    } else {
        var json_url = 'json/blog.json';
    }

    fetch(json_url)
        .then(response => response.json())
        .then(data => {
            const blogContent = document.getElementById('blog-content');

            search = urlParams.get('search')
            if (search) {
                blogContent.innerHTML = `<h2>BLOG列表</h2>
                <input type="text" id="searchInput" placeholder="搜索Blog">
                <button id="searchButton" style="margin-bottom:10px;margin-left:10px;">搜索</button>
                <button id="clearSearch" style="margin-left:10px;">清除搜索</button>`;
                searchInput.value = search;
                clearSearch.addEventListener('click', () => {
                    window.location.search = `json=${json_url}`;
                });

            } else {
                blogContent.innerHTML = `<h2>BLOG列表</h2>
                <input type="text" id="searchInput" placeholder="搜索Blog">
                <button id="searchButton" style="margin-bottom:10px;margin-left:10px;">搜索</button>`;
            }
            searchInput.addEventListener('keyup', (event) => {
                if (event.key === 'Enter') {
                    window.location.search = `json=${json_url}&search=${encodeURIComponent(searchInput.value.trim())}`;
                }
            });
            searchButton.addEventListener('click', () => {
                window.location.search = `json=${json_url}&search=${encodeURIComponent(searchInput.value.trim())}`;
            });

            data.blogs.forEach(blog => {
                if (search) {
                    const regex = new RegExp(`(${search})`, 'gi'); // 不区分大小写的正则表达式
                    if (!(String(blog.title).includes(search) || String(blog.preview).includes(search))) {
                        return; //跳出本次forEach循环
                    } else {
                        //将搜索关键字突出显示
                        if (String(blog.title).includes(search)) { blog.title = blog.title.replace(regex, '<span style="background-color: #98cdff;border: 1px solid #399fff;">\$1</span>'); }
                        if (String(blog.preview).includes(search)) { blog.preview = blog.preview.replace(regex, '<span style="background-color: #98cdff;border: 1px solid #399fff;">\$1</span>'); }
                    }
                }
                const blogPost = document.createElement('div');
                blogPost.classList.add('blog-post');
                blogPost.id = `blog-${blog.id}`;

                if (("coverImage" in blog) && ("preview" in blog)) {
                    blogPost.innerHTML = `
                        <h3 style="margin-top:0;">${blog.title}</h3>
                        <p style="color:#444444;margin: 10px 0;">${blog.preview}</p>
                        <img src="${blog.coverImage}" alt="${blog.coverImage}" style="max-height: 200px; min-width:100px; height: auto;"/>
                        <p style="color:#999999;margin: 10px 0;">${blog.date}</p>
                    `;
                } if (!("coverImage" in blog) && ("preview" in blog)) {
                    blogPost.innerHTML = `
                        <h3 style="margin-top:0;">${blog.title}</h3>
                        <div style="color:#444444;">${blog.preview}</div>
                        <p style="color:#999999;margin: 10px 0;">${blog.date}</p>
                    `;
                } if (("coverImage" in blog) && !("preview" in blog)) {
                    blogPost.innerHTML = `
                        <h3 style="margin-top:0;">${blog.title}</h3>
                        <img src="${blog.coverImage}" alt="${blog.coverImage}" style="max-height: 200px; min-width:100px; height: auto;"/>
                        <p style="color:#999999;margin: 10px 0;">${blog.date}</p>
                    `;
                } if (!("coverImage" in blog) && !("preview" in blog)) {
                    blogPost.innerHTML = `
                        <h3 style="margin-top:0;">${blog.title}</h3>
                        <p style="color:#999999;margin: 10px 0;">${blog.date}</p>
                    `;
                }

                // blog框右下角提示
                const goto = document.createElement('span');
                goto.classList.add('goto');
                goto.textContent = "点击查看";
                blogPost.appendChild(goto);

                blogPost.style.setProperty('--accentcolor', "#399fff");
                blogPost.style.setProperty('--bgcolor', "#d6e5f3");
                if (("special" in blog)) {
                    if ("hidden" in blog.special) {
                        if (blog.special.hidden) {
                            console.log(`<log> id为[${blog.id}]的BLOG被隐藏\n      原因:此blog中出现"hidden"的special标签`); //当blog中出现"hidden"的special标签,在列表中不显示此条blog(但可以用链接访问)
                            return; //跳出本次forEach循环
                        }
                    }
                    if ("password" in blog.special) {
                        blogPost.innerHTML += "\n<span style='color:#ff5e5e;border: 1px solid #ff5e5e;background-color:hsla(0, 100%, 90%, 0.6);border-radius:4px;'>需要秘钥</span>"
                    }
                    if ("tag" in blog.special) {
                        const tag = document.createElement('span');
                        tag.classList.add('tag');
                        tag.textContent = blog.special.tag[0];
                        tag.style.setProperty('--tagbgcolor', blog.special.tag[1]);
                        tag.style.setProperty('--tagtextcolor', blog.special.tag[2]);
                        
                        blogPost.appendChild(tag);
                    }
                    if ("accentcolor" in blog.special) {
                        blogPost.style.setProperty('--accentcolor', blog.special.accentcolor);
                    }
                    if ("bgcolor" in blog.special) {
                        blogPost.style.setProperty('--bgcolor', blog.special.bgcolor);
                    }
                    if ("collection" in blog.special) {
                        blogPost.addEventListener('click', () => {
                            window.location.href = `?json=${blog.special.collection}`;
                        });
                        blogContent.appendChild(blogPost);
                        return; //跳出本次forEach循环
                    }
                }

                if (urlParams.get('json')) {
                    blogPost.addEventListener('click', () => {
                        window.location.href = `blog.html?json=${json_url}&id=${blog.id}`;
                    });
                } else {
                    blogPost.addEventListener('click', () => {
                        window.location.href = `blog.html?id=${blog.id}`;
                    });
                }

                blogContent.appendChild(blogPost);
            });

            const blogEnd = document.createElement('div');
            blogEnd.innerHTML = "- 再怎么找也没有啦 -";
            blogEnd.style.textAlign = "center";
            blogEnd.style.color = "#aaa";
            blogEnd.style.paddingBottom = "10px";
            blogContent.appendChild(blogEnd);
        })
        .catch(error => {
            console.error('加载BLOG数据异常:', error);
            document.getElementById('blog-content').innerHTML = `<p>加载BLOG数据异常[${error}]</p><br><span>尝试<a href="index.html">返回主页</a>或看看控制台有没有报错?</span>`;
        });
});
