#!/usr/bin/env python3
"""导出聊天记录为 ChatLab 格式（JSON/JSONL），无需浏览器。

用法:
  python3 export.py                              # 导出最近会话为 JSONL
  python3 export.py --filter "会话名称"           # 导出指定会话
  python3 export.py --filter "会话名称" --format json  # 导出为 JSON
  python3 export.py --output data/my_export.jsonl      # 指定输出路径
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from extractor.exporter import ChatLabExporter


def main():
    name_filter = None
    output_format = "jsonl"
    output_path = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--filter" and i + 1 < len(args):
            name_filter = args[i + 1]
            i += 2
        elif args[i] == "--format" and i + 1 < len(args):
            output_format = args[i + 1]
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__.strip())
            return
        else:
            i += 1

    ext = ".json" if output_format == "json" else ".jsonl"
    output_path = output_path or os.path.join("data", f"export{ext}")

    exporter = ChatLabExporter(conv_name=name_filter, output_format=output_format)
    exporter.export(output_path)


if __name__ == "__main__":
    main()
