#!/usr/bin/env python3
"""导出聊天记录为 ChatLab 格式（JSON/JSONL），无需浏览器。

用法:
  python3 export.py                              # 导出最近会话为 JSONL
  python3 export.py --filter "会话名称"           # 导出指定会话
  python3 export.py --filter "会话名称" --format json  # 导出为 JSON
  python3 export.py --output data/my_export.jsonl      # 指定输出路径
  python3 export.py --asr-url http://127.0.0.1:8000    # 导出时转写语音

ASR 可选参数:
  --asr-url URL             Qwen3-ASR Custom Server 根地址
  --asr-language LANGUAGE   语言名称/代码；auto 表示自动检测（默认 Chinese）
  --asr-prompt TEXT         人名、领域词等识别提示
  --asr-timeout SECONDS     单次请求超时（默认 300）
  --asr-batch-size N        每批文件数（默认/上限 10；设为 1 使用单文件接口）

也可使用 QWEN_ASR_URL、QWEN_ASR_LANGUAGE、QWEN_ASR_PROMPT、
QWEN_ASR_TIMEOUT、QWEN_ASR_BATCH_SIZE 环境变量。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from extractor.exporter import ChatLabExporter


def main():
    name_filter = None
    output_format = "jsonl"
    output_path = None
    asr_url = None
    asr_language = None
    asr_prompt = None
    asr_timeout = None
    asr_batch_size = None

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
        elif args[i] == "--asr-url" and i + 1 < len(args):
            asr_url = args[i + 1]
            i += 2
        elif args[i] == "--asr-language" and i + 1 < len(args):
            value = args[i + 1]
            asr_language = "" if value.lower() in ("auto", "none", "detect") else value
            i += 2
        elif args[i] == "--asr-prompt" and i + 1 < len(args):
            asr_prompt = args[i + 1]
            i += 2
        elif args[i] == "--asr-timeout" and i + 1 < len(args):
            asr_timeout = args[i + 1]
            i += 2
        elif args[i] == "--asr-batch-size" and i + 1 < len(args):
            asr_batch_size = args[i + 1]
            i += 2
        elif args[i] in ("-h", "--help"):
            print(__doc__.strip())
            return
        else:
            i += 1

    ext = ".json" if output_format == "json" else ".jsonl"
    output_path = output_path or os.path.join("data", f"export{ext}")

    exporter = ChatLabExporter(
        conv_name=name_filter,
        output_format=output_format,
        asr_url=asr_url,
        asr_language=asr_language,
        asr_prompt=asr_prompt,
        asr_timeout=asr_timeout,
        asr_batch_size=asr_batch_size,
    )
    exporter.export(output_path)


if __name__ == "__main__":
    main()
