"""生成 README 里的 CI 验收网关演示 GIF。

直接运行真实命令并把终端输出逐行渲染成动图，避免手工拼接截图。
只在更新文档时需要用到，运行前装一次 Pillow：

    python3 -m pip install --user Pillow
    python3 docs/render_demo.py
"""
from __future__ import annotations

import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "ci-gate-demo-4.gif")

FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
CJK_FONT_PATH = "/System/Library/Fonts/Hiragino Sans GB.ttc"
FONT_SIZE = 18
LINE_HEIGHT = 26
PADDING = 18
CHROME_HEIGHT = 34
WIDTH = 980
ROWS = 26
FRAME_MS = 260
HOLD_MS = 2600

BG = (13, 17, 23)
CHROME = (22, 27, 34)
GRAY = (185, 192, 200)
WHITE = (240, 246, 252)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 168, 60)
CYAN = (121, 192, 255)
DIM = (110, 118, 129)

COMMANDS = [
    ["python3", "agent-harness/examples/ci_gate.py", "--mock"],
    ["python3", "agent-harness/examples/ci_gate.py", "--mock", "--retries", "0"],
]


def color_for(line: str):
    if line.startswith("$ "):
        return WHITE
    if "通过，允许交付" in line:
        return GREEN
    if "拦下，不允许交付" in line or line.startswith(("FAIL", "FAILED", "AssertionError")):
        return RED
    if "[ci-check]" in line or line.startswith("修正提示") or "尚未达到完成标准" in line:
        return YELLOW
    if line.startswith(("状态", "模型轮次", "防线打回", "网关结论", "工作区", "判据", "目标", "交接记录")):
        return CYAN
    if set(line.strip()) in ({"="}, {"-"}) and line.strip():
        return DIM
    return GRAY


def collect_lines() -> list[str]:
    lines: list[str] = []
    for command in COMMANDS:
        lines.append("$ " + " ".join(command))
        proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        for raw in (proc.stdout + proc.stderr).splitlines():
            lines.append(raw.rstrip())
        lines.append(f"$ echo $?  ->  {proc.returncode}")
        lines.append("")
    return lines


def truncate(measure, text: str, max_width: int) -> str:
    if measure(text) <= max_width:
        return text
    while text and measure(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def render(lines: list[str]) -> None:
    # Menlo 没有中文字形，中日韩字符改用 Hiragino，按字符切换字体避免出现方块
    mono = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    cjk = ImageFont.truetype(CJK_FONT_PATH, FONT_SIZE)
    baseline = mono.getmetrics()[0]

    def font_for(char: str):
        return cjk if ord(char) > 0x2E7F else mono

    height = CHROME_HEIGHT + PADDING * 2 + ROWS * LINE_HEIGHT
    max_text_width = WIDTH - PADDING * 2

    def frame(visible: list[str]) -> Image.Image:
        image = Image.new("RGB", (WIDTH, height), BG)
        draw = ImageDraw.Draw(image)

        def measure(text: str) -> float:
            return sum(draw.textlength(char, font=font_for(char)) for char in text)

        def write(x: float, y: int, text: str, fill) -> float:
            for char in text:
                font = font_for(char)
                draw.text((x, y + baseline), char, font=font, fill=fill, anchor="ls")
                x += draw.textlength(char, font=font)
            return x

        draw.rectangle([0, 0, WIDTH, CHROME_HEIGHT], fill=CHROME)
        for index, dot in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            cx = PADDING + index * 20
            draw.ellipse([cx, 12, cx + 11, 23], fill=dot)
        draw.text((WIDTH // 2 - 125, 9), "mini-coding-agent — ci gate demo", font=mono, fill=DIM)
        for row, line in enumerate(visible[-ROWS:]):
            y = CHROME_HEIGHT + PADDING + row * LINE_HEIGHT
            text = truncate(measure, line, max_text_width)
            if text.startswith("$ "):
                x = write(PADDING, y, "$ ", GREEN)
                write(x, y, text[2:], WHITE)
            else:
                write(PADDING, y, text, color_for(line))
        return image

    frames = [frame(lines[:i + 1]) for i in range(len(lines))]
    durations = [FRAME_MS] * len(frames)
    durations[-1] = HOLD_MS
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"{OUT}  {os.path.getsize(OUT) / 1024:.0f} KB  {len(frames)} frames")


if __name__ == "__main__":
    for path in (FONT_PATH, CJK_FONT_PATH):
        if not os.path.exists(path):
            print(f"缺少字体 {path}，请改成本机可用的等宽 / 中文字体路径。")
            sys.exit(1)
    render(collect_lines())
