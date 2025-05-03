import tkinter as tk
from tkinter import filedialog, messagebox
import json

# 全局变量，用于存储当前的json数据
json_data = {}
current_selected_index = None  # 用于记录当前选中的blog在列表中的索引
# 用于标记是否正在进行鼠标拖动操作，避免误判取消选中状态
is_dragging = False  
# 用于记录鼠标拖动操作的起始位置是否在列表框内
drag_started_in_listbox = True  

def load_json_file():
    """
    打开文件选择对话框，加载json文件并解析数据
    """
    global json_data, current_selected_index
    file_path = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
    if file_path:
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                json_data = json.load(file)
                update_listbox()
                current_selected_index = None
        except json.JSONDecodeError:
            messagebox.showerror("错误", "无效的JSON文件格式")

def save_json_file():
    """
    将当前编辑后的json数据保存为文件
    """
    global json_data
    file_path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Files", "*.json")])
    if file_path:
        try:
            blogs = []
            for index, blog in enumerate(json_data.get("blogs", [])):
                if index == current_selected_index:
                    blogs.append(blog)
                else:
                    blogs.append(blog)
            json_data["blogs"] = blogs
            # 明确指定以utf-8编码打开文件进行写入，确保保存为utf-8格式
            with open(file_path, 'w', encoding='utf-8') as file:
                json.dump(json_data, file, indent=4, ensure_ascii=False)
            messagebox.showinfo("提示", "文件保存成功")
        except:
            messagebox.showerror("错误", "保存文件时出现问题")

def update_listbox():
    """
    根据当前json数据中的blogs列表更新列表框显示内容
    """
    listbox.delete(0, tk.END)
    for blog in json_data.get("blogs", []):
        listbox.insert(tk.END, blog.get("title", ""))

def add_blog():
    """
    添加一个新的blog元素到json数据的blogs列表中
    """
    global json_data
    new_blog = {
        "id": "",
        "title": "",
        "date": "",
        "content": [],
        "special": {}
    }
    json_data.setdefault("blogs", []).append(new_blog)
    update_listbox()

def delete_blog():
    """
    根据选中的列表框项删除对应的blog元素
    """
    global json_data, current_selected_index
    selected_index = listbox.curselection()
    if selected_index:
        del json_data["blogs"][selected_index[0]]
        update_listbox()
        current_selected_index = None
    else:
        messagebox.showwarning("警告", "请先选择要删除的博客项")

def update_selected_blog():
    """
    根据输入框的值更新选中的blog元素的各个字段值
    """
    global json_data, current_selected_index
    selected_index = listbox.curselection()
    if selected_index:
        blog = json_data["blogs"][selected_index[0]]
        blog["id"] = id_entry.get()
        blog["title"] = title_entry.get()
        blog["date"] = date_entry.get()
        preview_text = preview_entry.get("1.0", tk.END)  # 修改此处，添加正确的索引参数
        if preview_text:
            blog["preview"] = preview_text
        else:
            if "preview" in blog:
                del blog["preview"]
        blog["content"] = content_text.get("1.0", tk.END).splitlines()
        special_text = special_text_widget.get("1.0", tk.END).strip()
        if special_text:
            try:
                special_data = json.loads(special_text)
                blog["special"] = special_data
            except json.JSONDecodeError:
                messagebox.showerror("错误", "输入的Special内容不是合法的JSON格式")
        else:
            if "special" in blog:
                del blog["special"]

        update_listbox()
        current_selected_index = selected_index[0]
    else:
        messagebox.showwarning("警告", "请先选择要更新的blog项")

def show_detail(event):
    """
    当在列表框中选中某个blog时，在下方文本框中显示其详细信息
    """
    global current_selected_index
    selected_index = listbox.curselection()
    if selected_index:
        current_selected_index = selected_index[0]
        blog = json_data["blogs"][current_selected_index]
        update_detail_text(blog)
    else:
        update_detail_text()

def update_detail_text(blog=None):
    """
    更新详情文本框的内容，显示选中blog的完整信息或者清空内容
    """
    global current_selected_index
    if blog:
        preview_entry.config(state=tk.NORMAL)
        preview_entry.delete("1.0", tk.END)
        preview_entry.insert(tk.END, json.dumps(blog, indent=4, ensure_ascii=False))
        preview_entry.config(state=tk.DISABLED)
        preview_entry.tag_config("gray", foreground="gray")
        preview_entry.tag_add("gray", "1.0", tk.END)

        # 更新content框内容，保持可编辑状态
        content_text.delete("1.0", tk.END)
        content_text.insert(tk.END, "\n".join(blog.get("content", [])))

        # 更新special框内容，保持可编辑状态
        special_text_widget.delete("1.0", tk.END)
        special_data = blog.get("special", {})
        if special_data:
            special_text_widget.insert(tk.END, json.dumps(special_data, indent=4))

        # 更新ID、标题、日期信息的显示
        id_entry.delete(0, tk.END)
        id_entry.insert(0, blog.get("id", ""))
        title_entry.delete(0, tk.END)
        title_entry.insert(0, blog.get("title", ""))
        date_entry.delete(0, tk.END)
        date_entry.insert(0, blog.get("date", ""))

    else:
        preview_entry.config(state=tk.NORMAL)
        preview_entry.delete("1.0", tk.END)
        preview_entry.config(state=tk.DISABLED)
        content_text.delete("1.0", tk.END)
        special_text_widget.delete("1.0", tk.END)
        id_entry.delete(0, tk.END)
        title_entry.delete(0, tk.END)
        date_entry.delete("1.0", tk.END)
        current_selected_index = None

def on_mouse_drag_start(event):
    """
    鼠标拖动开始时的事件处理函数，标记开始拖动，并记录起始位置是否在列表框内
    """
    global is_dragging, drag_started_in_listbox
    is_dragging = True
    drag_started_in_listbox = event.widget == listbox

def on_mouse_drag_stop(event):
    """
    鼠标拖动停止时的事件处理函数，标记结束拖动，并根据情况恢复选中状态
    """
    global is_dragging, current_selected_index, drag_started_in_listbox
    is_dragging = False
    if current_selected_index is not None and not drag_started_in_listbox:
        listbox.selection_set(current_selected_index)
    drag_started_in_listbox = True

def bind_drag_events(widget):
    """
    为给定的widget及其所有子部件绑定鼠标拖动开始和停止事件
    """
    widget.bind("<ButtonPress-1>", on_mouse_drag_start)
    widget.bind("<ButtonRelease-1>", on_mouse_drag_stop)
    for child in widget.winfo_children():
        bind_drag_events(child)

def update_preview():
    """
    点击“更新预览”按钮时执行的函数，根据输入框中正在修改的内容构建临时blog数据并显示在预览框中
    """
    global current_selected_index
    if current_selected_index is not None:
        # 构建临时blog数据，包含从输入框获取的正在修改的值
        temp_blog = {
            "id": id_entry.get(),
            "title": title_entry.get(),
            "date": date_entry.get(),
            "content": content_text.get("1.0", tk.END).splitlines(),
            "preview": preview_entry.get("1.0", tk.END),
            "special": {}
        }
        special_text = special_text_widget.get("1.0", tk.END).strip()
        if special_text:
            try:
                special_data = json.loads(special_text)
                temp_blog["special"] = special_data
            except json.JSONDecodeError:
                messagebox.showerror("错误", "输入的Special内容不是合法的JSON格式")

        preview_entry.config(state=tk.NORMAL)
        preview_entry.delete("1.0", tk.END)
        preview_entry.insert(tk.END, json.dumps(temp_blog, indent=4, ensure_ascii=False))
        preview_entry.config(state=tk.DISABLED)
        preview_entry.tag_config("gray", foreground="gray")
        preview_entry.tag_add("gray", "1.0", tk.END)


root = tk.Tk()
root.title("JSON文件编辑器")

# 创建加载文件按钮
load_button = tk.Button(root, text="加载JSON文件", command=load_json_file)
load_button.pack(pady=10)

# 创建保存文件按钮
save_button = tk.Button(root, text="保存JSON文件", command=save_json_file)
save_button.pack(pady=10)

# 创建列表框用于显示blogs中的各个元素（以title展示）
listbox = tk.Listbox(root)
listbox.pack(pady=10)
listbox.bind("<<ListboxSelect>>", show_detail)  # 绑定选中事件
bind_drag_events(root)  # 为整个窗口及其子部件绑定鼠标拖动事件

# 创建按钮用于添加和删除blog元素
add_button = tk.Button(root, text="添加博客", command=add_blog)
add_button.pack(pady=5)
delete_button = tk.Button(root, text="删除博客", command=delete_blog)
delete_button.pack(pady=5)

# 创建输入框用于编辑选中blog元素的各个字段
id_label = tk.Label(root, text="ID:")
id_label.pack()
id_entry = tk.Entry(root)
id_entry.pack()

title_label = tk.Label(root, text="标题:")
title_label.pack()
title_entry = tk.Entry(root)
title_entry.pack()

date_label = tk.Label(root, text="日期:")
date_label.pack()
date_entry = tk.Entry(root)
date_entry.pack()

# 创建文本框用于输入content内容，保持可编辑状态
content_text_label = tk.Label(root, text="内容")
content_text_label.pack()
content_text = tk.Text(root, height=5)
content_text.pack(pady=10)

# 创建文本框用于输入special内容，保持可编辑状态
special_text_widget_label = tk.Label(root, text="特殊信息")
special_text_widget_label.pack()
special_text_widget = tk.Text(root, height=5)
special_text_widget.pack(pady=10)

# 创建输入框用于编辑预览信息，并设置为灰色显示
preview_label = tk.Label(root, text="预览")
preview_label.pack()
preview_entry = tk.Text(root, height=5)
preview_entry.pack(pady=10)

# 创建“更新预览”按钮并绑定相应的点击事件处理函数
update_preview_button = tk.Button(root, text="更新预览", command=update_preview)
update_preview_button.pack(pady=5)

# 创建更新按钮，用于将输入框的值更新到选中的blog元素中
update_button = tk.Button(root, text="更新选中博客", command=update_selected_blog)
update_button.pack(pady=10)

root.mainloop()