import json

print("输入内容，输入完毕后请按 Ctrl+D (Unix/Linux) 或 Ctrl+Z (Windows) 并按回车键结束输入：")
user_input = []
try:
    while True:
        line = input()
        if line.strip():  # 忽略空行
            user_input.append(line)
except EOFError:
    pass

data = {
    "content": user_input
}

print(user_input)
with open("tool/MD转json/out.json", "w", encoding="utf-8") as file:
    json.dump(data, file, ensure_ascii=False, indent=4)

print("内容已保存到 out.json 文件中。")