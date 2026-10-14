from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
WIDTH = 1200
BG = "#101b2b"
PANEL = "#1b2a3e"
EDGE = "#34465e"
WHITE = "#f3f5f7"
MUTED = "#b7c4d6"
MINT = "#8be3c2"
AMBER = "#ffca85"
RED = "#ffaaa0"
BLUE = "#9fc5ff"
FONT_ROOT = Path("/usr/share/fonts/truetype/lato")


def font(size, bold=False):
    return ImageFont.truetype(str(FONT_ROOT / f"Lato-{'Bold' if bold else 'Regular'}.ttf"), size)


def text(draw, xy, value, size=24, color=WHITE, bold=False):
    draw.text(xy, value, font=font(size, bold), fill=color)


def panel(draw, box, color=EDGE, fill=PANEL, width=2, radius=18):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=color, width=width)


def arrow(draw, start, end, color):
    draw.line((start, end), fill=color, width=3)
    x, y = end
    draw.polygon(((x, y), (x - 8, y - 5), (x - 8, y + 5)), fill=color)


def pill(draw, x, y, label, color=MINT, size=21):
    width = int(draw.textlength(label, font=font(size, True))) + 28
    panel(draw, (x, y, x + width, y + 37), color=color, radius=10)
    text(draw, (x + 14, y + 5), label, size, color, True)


def canvas(height, category, title, subtitle):
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 34, 47, 59), radius=3, fill=MINT)
    text(draw, (60, 32), category, 20, MINT, True)
    text(draw, (40, 78), title, 46, WHITE, True)
    text(draw, (40, 141), subtitle, 25, MUTED)
    return image, draw


def workflow(stage, phase=0):
    image, draw = canvas(
        470,
        "GROUNDED BUILD  /  HOW IT WORKS",
        "One snapshot. Independent scrutiny.",
        "A reviewed plan, isolated implementation, and evidence you can inspect.",
    )
    headings = ["Frozen SHA", "Two planners", "Reviewed plan", "Build + review", "Integrate"]
    lines = [
        ["Same repository", "Recorded scope"],
        ["A investigates", "B investigates", "Cross-review"],
        ["Host synthesis", "Fresh reviewer"],
        ["Isolated batches", "Fixed-SHA review"],
        ["User approval", "Reviewed commit"],
    ]
    for i in range(5):
        x = 40 + i * 230
        active = stage == i
        color = MINT if active else EDGE
        panel(draw, (x, 206, x + 200, 365), color, width=3 if active else 2)
        text(draw, (x + 16, 220), f"0{i + 1}", 20, MINT if active else MUTED, True)
        text(draw, (x + 16, 252), headings[i], 25, WHITE, True)
        for row, label in enumerate(lines[i]):
            text(draw, (x + 16, 292 + row * 22), label, 20, MUTED)
        if i < 4:
            arrow(draw, (x + 205, 281), (x + 225, 281), MINT if stage > i else EDGE)
            if active:
                px = x + 205 + int(phase * 17)
                draw.ellipse((px - 3, 278, px + 3, 284), fill=WHITE)
    captions = [
        "Freeze the Git baseline and the authorized request.",
        "Investigate independently; cross-check claims against evidence.",
        "Synthesize a plan; review it before implementation approval.",
        "Implement each batch; bind review and verification to exact commits.",
        "Integrate the reviewed candidate only with explicit user approval.",
        "Workflow state determines the next action. Every verdict has evidence.",
    ]
    text(draw, (40, 401), captions[stage], 24, MINT)
    for i in range(6):
        x = 40 + i * 188
        draw.rounded_rectangle((x, 450, x + 174, 454), radius=2, fill=MINT if i == stage else EDGE)
    return image


def comparison(stage, phase=0):
    image, draw = canvas(
        620,
        "EXECUTED CASE  /  SYNTHETIC DEFECT, ACTUAL RUN",
        "Cancelled means cancelled.",
        "The same cancellation request. The same late worker result. Different outcomes.",
    )
    labels = ["01  Cancel accepted", "02  Worker returns", "03  Read the snapshot"]
    for i, label in enumerate(labels):
        x = 40 + i * 380
        panel(draw, (x, 193, x + 360, 237), MINT if i == stage else EDGE, radius=10)
        text(draw, (x + 16, 202), label, 22, MINT if i == stage else MUTED, True)
    settled = stage != 0
    columns = [
        (40, "BEFORE", RED, "completed" if settled else "cancelled", '"late result"' if settled else "None"),
        (620, "AFTER", MINT, "cancelled", "None"),
    ]
    for x, label, accent, status, result in columns:
        panel(draw, (x, 260, x + 540, 518))
        text(draw, (x + 24, 280), label, 21, accent, True)
        text(draw, (x + 24, 318), "cancel() accepted: True", 25, WHITE, True)
        draw.line((x + 24, 360, x + 516, 360), fill=EDGE, width=2)
        text(draw, (x + 24, 380), "status", 24, MUTED)
        pill(draw, x + 170, 375, status, accent if settled else BLUE, 28)
        text(draw, (x + 24, 435), "result", 24, MUTED)
        text(draw, (x + 184, 433), result, 28, accent if settled else WHITE, True)
        description = (
            "Cancellation overwritten" if label == "BEFORE" else "Late result suppressed"
        ) if settled else "Cancellation accepted; worker still running"
        text(draw, (x + 24, 483), description, 22, accent if settled else MUTED)
    if stage == 2:
        text(draw, (40, 548), "5 tests passed. The defect remained.", 25, RED, True)
        text(draw, (620, 548), "17 tests pass. Reproducer passes.", 25, MINT, True)
    elif stage == 1:
        text(draw, (40, 548), "A late return changed the public outcome.", 25, RED)
        text(draw, (620, 548), "Accepted cancellation stays final.", 25, MINT)
    else:
        text(draw, (40, 548), "The defect appears after the worker exits.", 25, MUTED)
        text(draw, (620, 548), "Both requests start with accepted: True.", 25, MUTED)
    text(draw, (40, 592), "Illustrated from retained evidence. Animation timing is illustrative.", 18, MUTED)
    return image


def save_animation(name, renderer, stages, hold_ms):
    frames = []
    durations = []
    for stage in stages:
        # Four subtle motion frames, then a long hold for reading.
        for phase in range(4):
            frames.append(renderer(stage, phase / 3).quantize(colors=96))
            durations.append(120 if phase < 3 else hold_ms)
    frames[0].save(
        ROOT / f"{name}.gif", save_all=True, append_images=frames[1:],
        duration=durations, loop=0, optimize=True, disposal=1,
    )
    renderer(stages[0]).save(ROOT / f"{name}.png", optimize=True)


if __name__ == "__main__":
    # Open on the complete result, so the first frame also works as a still.
    save_animation("workflow", workflow, [5, 0, 1, 2, 3, 4], 2200)
    save_animation("cancellation", comparison, [2, 0, 1, 2], 2800)
