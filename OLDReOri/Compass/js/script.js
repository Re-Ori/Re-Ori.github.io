fetch('config.json')
    .then(response => response.json())
    .then(config => {
        const {
            rows,
            columns,
            gapSize,
            showDividers,
            borderWidth,
            background: {
                image: backgroundImage,
                filterColor,
                blurRadius
            }
        } = config;

        const container = document.getElementById('container');
        const grid = document.getElementById('grid');

        container.style.setProperty('--border-width', `${borderWidth}px`);
        grid.style.setProperty('--gap-size', `${gapSize}px`);

        // 设置背景属性
        container.style.setProperty('--background-image', `url('${backgroundImage}')`);
        container.style.setProperty('--filter-color', filterColor);
        container.style.setProperty('--blur-radius', `${blurRadius}px`);

        // 配置网格
        grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
        grid.style.gridTemplateColumns = `repeat(${columns}, 1fr)`;

        // 创建网格项
        const totalItems = rows * columns;
        for (let i = 0; i < totalItems; i++) {
            grid.appendChild(document.createElement('div')).className = 'grid-item';
        }

        // 设置分割线显示
        if (showDividers) {
            grid.classList.add('show-divider');

            const markBoundary = (index) => {
                grid.children[index].classList.add('boundary-border');
            };

            // 四周边界设置
            for (let row = 0; row < rows; row++) {
                markBoundary(row * columns);
                markBoundary(row * columns + columns - 1);
            }
            for (let col = 0; col < columns; col++) {
                markBoundary(col);
                markBoundary((rows - 1) * columns + col);
            }

            // 中间单元格边界
            for (let row = 1; row < rows; row++) {
                for (let col = 1; col < columns; col++) {
                    const index = row * columns + col;
                    const cell = grid.children[index];
                    cell.style.borderLeft = '1px dotted black';
                    cell.style.borderTop = '1px dotted black';
                }
            }
        }

        // 计算网格项尺寸
        const gridRect = grid.getBoundingClientRect();
        const itemWidth = (gridRect.width - (columns - 1) * gapSize) / columns;
        const itemHeight = (gridRect.height - (rows - 1) * gapSize) / rows;

        fetch('tags.json')
            .then(response => response.json())
            .then(tags => {
                // 添加标签
                tags.slice().reverse().forEach(tag => {
                    const tagEl = document.createElement('div');
                    tagEl.className = 'tag';
                    tagEl.id = 'tag-' + tag.id;
                    tagEl.style.pointerEvents = 'auto';

                    // 解析 position 字符串
                    const [colStart, colEnd, rowStart, rowEnd] = tag.position.split(',').map(Number);
                    
                    // 计算位置和尺寸
                    const colCount = colEnd - colStart + 1;
                    const rowCount = rowEnd - rowStart + 1;
                    const left = (colStart - 1) * (itemWidth + gapSize);
                    const top = (rowStart - 1) * (itemHeight + gapSize);
                    const width = colCount * itemWidth + (colCount - 1) * gapSize;
                    const height = rowCount * itemHeight + (rowCount - 1) * gapSize;

                    Object.assign(tagEl.style, {
                        left: `${left}px`,
                        top: `${top}px`,
                        width: `${width}px`,
                        height: `${height}px`,
                        backgroundColor: tag.colors.bgColor,
                        color: tag.colors.color,
                        borderRadius: `${gapSize}px`,
                        ...tag.style
                    });

                    // 创建icon元素
                    if (tag.icon && tag.icon.url) {
                        const icon = document.createElement('img');
                        icon.src = tag.icon.url;
                        icon.style.width = `${tag.icon.size}px`;
                        icon.style.height = `${tag.icon.size}px`;
                        icon.style.display = 'block';
                        icon.style.margin = '0 auto';
                        icon.style.marginBottom = '5px';
                        tagEl.appendChild(icon);
                    }

                    // 设置内容并调整位置
                    const textContainer = document.createElement('div');
                    textContainer.textContent = tag.content;
                    // 修改布局为垂直排列
                    tagEl.style.flexDirection = 'column';
                    textContainer.style.textAlign = 'center';
                    tagEl.appendChild(textContainer);

                    // 悬停效果
                    if (tag.colors.hoverColor || tag.style.hover) {
                        tagEl.addEventListener('mouseenter', () => {
                            tagEl.style.color = tag.colors.hoverColor || tagEl.style.color;
                            tagEl.style.backgroundColor = tag.colors.bgHoverColor || tagEl.style.backgroundColor;
                            
                            if (tag.style.hover) {
                                Object.assign(tagEl.style, tag.style.hover);
                            }
                        });
                        tagEl.addEventListener('mouseleave', () => {
                            tagEl.style.color = tag.colors.color;
                            tagEl.style.backgroundColor = tag.colors.bgColor;
                            
                            // 恢复原始样式
                            Object.assign(tagEl.style, tag.style);
                        });
                    }

                    // 添加点击事件监听
                    if (tag.link) {
                        tagEl.addEventListener('click', () => {
                            window.location.href = tag.link;
                        });
                    }

                    grid.appendChild(tagEl);
                });
            })
            .catch(console.error);

    })
    .catch(console.error);