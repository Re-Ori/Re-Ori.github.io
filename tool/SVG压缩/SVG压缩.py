import re

def compress_svg_values(input_file, output_file, precision):
    # 读取SVG文件
    with open(input_file, 'r') as file:
        svg_data = file.read()

    # 定义正则表达式匹配数字，并限制精度
    pattern = re.compile(r'(?<=\D)(\d+\.\d+)(?=\D|$)')
    def format_match(match):
        num = float(match.group(0))
        return f"{num:.{precision}f}"

    # 替换SVG数据中的数字
    compressed_svg = pattern.sub(format_match, svg_data)

    # 写入压缩后的SVG文件
    with open(output_file, 'w') as file:
        file.write(compressed_svg)

# 示例用法
pre=1 # 精度,一般取1,但对于某些具有倾斜元素的svg可能会有位移,取值为3时基本无误差
compress_svg_values('tool/SVG压缩/in.svg', 'tool/SVG压缩/out['+str(pre)+'].svg',pre)
print("压缩svg数据已保存到 tool/SVG压缩/out["+str(pre)+"].svg 文件中")
