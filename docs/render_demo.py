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
OUT = os.path.join(ROOT, "docs", "ci-gate-demo.gif")

FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
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


def truncate(draw, font, text: str, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def render(lines: list[str]) -> None:
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    height = CHROME_HEIGHT + PADDING * 2 + ROWS * LINE_HEIGHT
    max_text_width = WIDTH - PADDING * 2

    def frame(visible: list[str]) -> Image.Image:
        image = Image.new("RGB", (WIDTH, height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, WIDTH, CHROME_HEIGHT], fill=CHROME)
        for index, dot in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            cx = PADDING + index * 20
            draw.ellipse([cx, 12, cx + 11, 23], fill=dot)
        draw.text((WIDTH // 2 - 90, 9), "aegis — ci gate demo", font=font, fill=DIM)
        for row, line in enumerate(visible[-ROWS:]):
            y = CHROME_HEIGHT + PADDING + row * LINE_HEIGHT
            text = truncate(draw, font, line, max_text_width)
            if text.startswith("$ "):
                draw.text((PADDING, y), "$", font=font, fill=GREEN)
                draw.text((PADDING + draw.textlength("$ ", font=font), y),
                          text[2:], font=font, fill=WHITE)
            else:
                draw.text((PADDING, y), text, font=font, fill=color_for(line))
        return image

    frames = [frame(lines[:i + 1]) for i in range(len(lines))]
    durations = [FRAME_MS] * len(frames)
    durations[-1] = HOLD_MS
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"{OUT}  {os.path.getsize(OUT) / 1024:.0f} KB  {len(frames)} frames")


if __name__ == "__main__":
    if not os.path.exists(FONT_PATH):
        print(f"缺少字体 {FONT_PATH}，请改成本机可用的等宽字体路径。")
        sys.exit(1)
    render(collect_lines())
