"""视觉小说外挂翻译窗:按键/鼠标侧键截取游戏台词区 → manga-ocr 识别日文 → SakuraLLM 翻译成中文;
日文原文标注假名,点词显示原形、读音和词义。"""
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import traceback
import tkinter.font as tkfont
import webbrowser
from collections import deque, namedtuple
from pathlib import Path
from urllib.parse import quote

import jaconv
import numpy as np
from PIL import Image, ImageGrab, ImageOps
from pynput import keyboard, mouse
from Xlib import X, display as xdisplay

HERE = Path(__file__).resolve().parent
MODEL = HERE / "models" / "Sakura-Galtransl-7B-v3.7.gguf"
CONFIG = HERE / "config.json"
GAME_ASPECT = 16 / 9
DEFAULT_REGION = [0.17, 0.76, 0.86, 0.99]  # 台词区在游戏画面里的相对位置(左,上,右,下),默认值按台词直接写在画面中下部的游戏调的
APP_TITLE = "VN 翻译"
HISTORY_LINES = 5
AUTO_POLL_MS = 500
SAMPLE_INTERVAL = 0.2  # 台词区留底截图的间隔(秒)
FRAME_KEEP = 8
CPU_THREADS = 4  # 别占满 CPU,否则 Wine 的声音线程抢不到时间,耳机里会噼啪响

SYSTEM_PROMPT = (
    "你是一个视觉小说翻译模型，可以通顺地使用给定的术语表以指定的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，注意不要混淆使役态和被动态的主语和宾语，"
    "不要擅自添加原文中没有的特殊符号，也不要擅自增加或减少换行。"
)


def load_config():
    # game_title: 游戏窗口标题里的一段文字,用来找游戏窗口,必须填
    # trigger_key: 单个字符或 pynput 键名(如 "f9");trigger_buttons: 鼠标侧键 button8(后退)/button9(前进)
    # readings: 读音修正表,分词词典读错的人名、专有词写在这里;dict_url: 「查词典」打开的网址,{word} 处填词
    # glossary: 术语表,每条「日文->中文 #备注」,整句翻译和查词都会参考;和中文同形但意思不同的词放这里
    cfg = {"game_title": "", "region": DEFAULT_REGION, "glossary": ["辟易->厌烦", "香具師->街头小贩 #在庙会、路边摆摊吆喝叫卖的人"], "geometry": None,
           "trigger_key": "t", "trigger_buttons": ["button8", "button9"],
           "readings": {"座敷童子": "ざしきわらし", "素": "す", "一寸": "ちょっと"}, "dict_url": "https://cjjc.weblio.jp/content/{word}"}
    if CONFIG.exists():
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        # 术语表和读音表与默认值合并,否则代码里后加的默认条目会被旧 config.json 里存的整张表盖掉
        saved["glossary"] = cfg["glossary"] + [g for g in saved.get("glossary", []) if g not in cfg["glossary"]]
        saved["readings"] = {**cfg["readings"], **saved.get("readings", {})}
        cfg.update(saved)
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def window_title(d, win):
    prop = win.get_full_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"))
    return prop.value.decode("utf-8", "ignore") if prop else ""


def active_window_title():
    d = xdisplay.Display()
    try:
        prop = d.screen().root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        if not prop or not prop.value[0]:
            return ""
        return window_title(d, d.create_resource_object("window", prop.value[0]))
    except Exception:  # 刚取到的前台窗口正好被关掉(BadWindow),当作没有前台窗口
        return ""
    finally:
        d.close()


def find_game_rect(game_title):
    """返回标题含 game_title 的窗口里游戏画面(去掉黑边后的 16:9 区域)在屏幕上的 (x, y, w, h),找不到返回 None。"""
    if not game_title:  # 空串会匹配任何窗口
        return None
    d = xdisplay.Display()
    try:
        root = d.screen().root
        clients = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        for wid in clients.value if clients else []:
            win = d.create_resource_object("window", wid)
            if game_title not in window_title(d, win):
                continue
            geo = win.get_geometry()
            pos = root.translate_coords(win, 0, 0)
            x, y, w, h = pos.x, pos.y, geo.width, geo.height
            if w / h > GAME_ASPECT:
                cw = round(h * GAME_ASPECT)
                return x + (w - cw) // 2, y, cw, h
            ch = round(w / GAME_ASPECT)
            return x, y + (h - ch) // 2, w, ch
    finally:
        d.close()
    return None


def region_bbox(rect, region):
    x, y, w, h = rect
    l, t, r, b = region
    return round(x + l * w), round(y + t * h), round(x + r * w), round(y + b * h)


def ink_mask(img):
    g = np.asarray(img.convert("L"), dtype=np.int16)
    return np.abs(g - np.median(g)) > 60


def white_fraction(arr):
    p = arr.reshape(-1, 3).astype(np.int16)
    return ((p.min(1) > 215) & (p.max(1) - p.min(1) < 30)).mean() if len(p) else 0.0


def is_ruby(box, boxes, ref_h):
    """注音小字:高度不到正文一半多一点,底边贴着某行正文的顶边,左右落在那行之内。ref_h 是最宽那行的高度。"""
    x0, x1, y0, y1 = box
    if y1 - y0 >= 0.6 * ref_h:
        return False
    return any(b1 - b0 >= 0.8 * ref_h and a0 - ref_h <= x0 and x1 <= a1 + ref_h and b0 - 0.5 * ref_h <= y1 <= b0 + 0.4 * ref_h
               for a0, a1, b0, b1 in boxes)


def split_lines(det, img):
    """用 RapidOCR 的检测模型找出每行字,同一高度的框(如人名和台词)合成一行,按从上到下返回。"""
    arr = np.asarray(img)
    boxes, _ = det(arr, use_det=True, use_cls=False, use_rec=False)
    boxes = sorted(((b[:, 0].min(), b[:, 0].max(), b[:, 1].min(), b[:, 1].max()) for b in map(np.array, boxes or [])),
                   key=lambda b: b[2])
    wide_lefts = [x0 for x0, x1, y0, y1 in boxes if x1 - x0 >= 2 * (y1 - y0)]
    widest = max(boxes, key=lambda b: b[1] - b[0], default=(0, 0, 0, 0))
    ref_h = widest[3] - widest[2]
    rows = []
    for x0, x1, y0, y1 in boxes:
        h = y1 - y0
        if is_ruby((x0, x1, y0, y1), boxes, ref_h):
            continue  # 游戏给汉字标的注音,不去掉会被当成一行拼到句首
        small = x1 - x0 < 2 * h
        aligned = any(abs(x0 - left) < 1.5 * h for left in wide_lefts)
        if small and not aligned and white_fraction(arr[int(y0):int(y1), int(x0):int(x1)]) < 0.03:
            continue  # 一两个字大小、不和台词左对齐、又没有白字像素,多半是背景花纹被误检
        if rows and abs((y0 + y1) / 2 - (rows[-1][2] + rows[-1][3]) / 2) < (y1 - y0) / 2:
            r = rows[-1]
            rows[-1] = [min(r[0], x0), max(r[1], x1), min(r[2], y0), max(r[3], y1)]
        else:
            rows.append([x0, x1, y0, y1])
    lines = []
    for x0, x1, y0, y1 in rows:
        h = y1 - y0
        box = (max(0, x0 - 0.6 * h), max(0, y0 - 0.15 * h), min(img.width, x1 + 0.6 * h), min(img.height, y1 + 0.15 * h))
        lines.append(img.crop(tuple(round(v) for v in box)))
    return lines


def text_mask(img):
    """白色笔画且 2 像素内有深色描边;大块白色背景(衣服、纸门)没有描边,不算。不是白字时退回 ink_mask。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    white = (a.min(2) > 200) & (a.max(2) - a.min(2) < 40)
    dark = a.max(2) < 70
    near = dark.copy()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            near |= np.roll(np.roll(dark, dy, 0), dx, 1)
    m = white & near
    return m if m.sum() > 0.01 * m.size else ink_mask(img)


def char_grid(line):
    """日文按等宽格子排版:在字高 0.8~1.35 倍里找格宽和起点,使格线上笔画最少,返回格线 x 坐标。
    不能按"最空的一列"切,は、い、に这类字中间本身就有空隙。"""
    m = text_mask(line)
    rows = np.where(m.any(1))[0]
    cols = np.where(m.any(0))[0]
    if len(cols) == 0:
        return None
    gh = rows[-1] - rows[0] + 1
    prof = m.sum(0).astype(float)
    best = None
    for p in np.arange(0.8 * gh, 1.35 * gh, 0.25):
        for phase in np.arange(0, p, 0.5):
            pos = np.arange(cols[0] - p + phase, cols[-1] + p, p).round().astype(int)
            pos = pos[(pos >= 0) & (pos < len(prof))]
            if len(pos) >= 3 and (best is None or prof[pos].mean() < best[0]):
                best = (prof[pos].mean(), pos)
    return best[1] if best else None


def fold_line(line, n, grid):
    """manga-ocr 会把图压成正方形,长条会被压坏;沿字格线把一行切成 n 段,上下叠成近方形的多行块。"""
    w, h = line.size
    if n <= 1:
        return line
    if grid is None:
        grid = np.array([round(k * w / n) for k in range(1, n)])
    cuts = sorted({0, w, *(int(grid[np.argmin(abs(grid - k * w / n))]) for k in range(1, n))})
    pieces = [line.crop((cuts[i], 0, cuts[i + 1], h)) for i in range(len(cuts) - 1)]
    bg = tuple(int(v) for v in np.median(np.asarray(line.convert("RGB")).reshape(-1, 3), 0))
    block = Image.new("RGB", (max(p.size[0] for p in pieces), h * len(pieces) + 8 * (len(pieces) - 1)), bg)
    for i, p in enumerate(pieces):
        block.paste(p, (0, i * (h + 8)))
    return block


def ocr_scored(mocr, img):
    """manga-ocr 识别一张图,同时返回逐字平均对数概率,作为模型对这次结果的把握程度。"""
    from manga_ocr.ocr import post_process

    x = mocr._preprocess(img.convert("L").convert("RGB"))
    # manga-ocr 自带 no_repeat_ngram_size=3,同一行里第二次出现「してい」「……」会被强行改成别的字(て→で),必须关掉
    out = mocr.model.generate(x[None].to(mocr.model.device), max_length=300, no_repeat_ngram_size=0,
                              output_scores=True, return_dict_in_generate=True)
    # manga-ocr 默认 4 路束搜索:要带上 beam_indices 才对得上最终选中的那一路;那一路提前结束的部分标为 -1,不计入平均
    scores = mocr.model.compute_transition_scores(out.sequences, out.scores, out.beam_indices)
    valid = out.beam_indices[0, :scores.shape[1]] >= 0
    text = post_process(mocr.tokenizer.decode(out.sequences[0].cpu(), skip_special_tokens=True))
    return text, float(scores[0][valid].mean())


def extra_close(chars):
    """「a」b」:两个」之间没有「,前一个」多半是认错的字,返回它的下标。"""
    for i, c in enumerate(chars):
        for l, r in ("「」", "『』"):
            if c == r and re.match(f"[^{l}{r}]+{r}", "".join(chars[i + 1:])):
                return i
    return None


def fix_quote(mocr, img):
    """manga-ocr 看到行首的「后,省略号后面的字容易被认成」(……一寸 → ……」寸);
    把多出来的」换成模型在那个位置的第二候选,从那里往后重新识别。"""
    import torch
    from manga_ocr.ocr import post_process

    x = mocr._preprocess(img.convert("L").convert("RGB"))[None].to(mocr.model.device)
    seq = mocr.model.generate(x, max_length=300, no_repeat_ngram_size=0)[0]
    i = extra_close(mocr.tokenizer.convert_ids_to_tokens(seq[1:].tolist()))
    if i is not None:
        with torch.no_grad():
            lp = mocr.model(pixel_values=x, decoder_input_ids=seq[None, :i + 1]).logits[0, -1].log_softmax(-1)
        lp[seq[i + 1]] = -float("inf")
        alt = int(lp.argmax())
        if lp[alt] > -5:  # 第二候选也很不像就不改
            prefix = torch.cat([seq[:i + 1], seq.new_tensor([alt])])[None]
            seq = mocr.model.generate(x, decoder_input_ids=prefix, max_length=300, no_repeat_ngram_size=0)[0]
    return post_process(mocr.tokenizer.decode(seq.cpu(), skip_special_tokens=True))


def ocr_line(mocr, line):
    """同一行用几种段数分别切开识别,取把握最高的;切口压到的字只在某一种切法里出错。"""
    w, h = line.size
    n = int(round(np.sqrt(w / h)))
    if w / h <= 3 or n <= 1:
        candidates = [(*ocr_scored(mocr, line), line)]
    else:
        grid = char_grid(line)
        candidates = [(*ocr_scored(mocr, block), block)
                      for block in (fold_line(line, k, grid) for k in sorted({max(2, n - 1), n, n + 1}))]
    text, _, img = max(candidates, key=lambda c: c[1])
    return fix_quote(mocr, img) if extra_close(text) is not None else text


def ocr_dialogue(det, mocr, img):
    texts = [ocr_line(mocr, line) for line in split_lines(det, img.convert("RGB"))]
    # 人名单独占第一行、下一行以「」或（）开头时,人名后加冒号:名字：「……」;Sakura 译文会照样保留冒号
    if len(texts) >= 2 and len(texts[0]) <= 8 and not re.search("[「」『』（）()]", texts[0]) \
            and texts[1].startswith(("「", "『", "（", "(")):
        texts[0] += "："
    text = re.sub("．{2,}", "……", "".join(texts))
    text = re.sub("(?<=[。」』！？…）])[☆★]+$", "", text)  # 句末等待点击的风车图标偶尔被认成☆
    return re.sub("(?<=[？！])・", "　", text)  # 问号后的全角空格常被认成「・」


Token = namedtuple("Token", "surface reading base base_reading pos")


def has_kanji(s):
    return any("一" <= c <= "鿿" or c == "々" for c in s)


def tokenize(tagger, text, readings):
    """切词并给出平假名读音(只给含汉字的词);readings 里的词整词匹配,优先于词典读音。"""
    def tag(seg):
        out = []
        for w in tagger(seg):
            f = w.feature
            kana = jaconv.kata2hira(f.kana) if f.kana and has_kanji(w.surface) else None
            if w.surface == "私" and kana == "わたくし":  # 词典默认读法太正式,台词里几乎都读わたし
                kana = "わたし"
            base = f.orthBase if f.orthBase and f.orthBase != "*" else w.surface
            base_kana = jaconv.kata2hira(f.kanaBase) if f.kanaBase and f.kanaBase != "*" else None
            if w.surface in readings:  # 分词器切出的整词正好在修正表里(单字的 素→す)
                kana = base_kana = readings[w.surface]
            out.append(Token(w.surface, kana, base, base_kana, f.pos1))
        return out

    # 两字以上的词先整词切出来(分词器会把人名 座敷童子 切碎);单字只改上面切出的整词,否则 素直 会被切成 素/直
    words = sorted((k for k in readings if len(k) > 1), key=len, reverse=True)
    if not words:
        return tag(text)
    tokens, pos = [], 0
    for m in re.finditer("|".join(map(re.escape, words)), text):
        tokens += tag(text[pos:m.start()])
        tokens.append(Token(m.group(), readings[m.group()], m.group(), readings[m.group()], "名詞"))
        pos = m.end()
    return tokens + tag(text[pos:])


def ruby_segments(surface, reading):
    """把词拆成汉字段和假名段,只给汉字段注音:取り扱い/とりあつかい → [(取,と),(り,None),(扱,あつか),(い,None)]。
    对不上(读音特殊)时整词注音。"""
    runs = re.findall("[一-鿿々]+|[^一-鿿々]+", surface)
    pattern = "".join("(.+?)" if has_kanji(r) else f"({re.escape(jaconv.kata2hira(r))})" for r in runs)
    m = re.fullmatch(pattern, reading)
    if not m:
        return [(surface, reading)]
    return [(r, g if has_kanji(r) else None) for r, g in zip(runs, m.groups())]


def bracketed(zh, src):
    """译文里第一对括号中的内容;模型会把【】换成『』〔〕等,原句里本来就有的括号不算(台词外层的「」)。"""
    for l, r in ("【】", "［］", "〔〕", "《》", "『』", "「」"):
        if l not in src:
            m = re.search(f"{l}([^{l}{r}]+){r}", zh)
            if m:
                return m.group(1)
    return None


class Translator:
    def __init__(self, glossary):
        import fugashi
        import torch
        from llama_cpp import Llama
        from manga_ocr import MangaOcr
        from rapidocr_onnxruntime import RapidOCR

        torch.set_num_threads(CPU_THREADS)
        self.det = RapidOCR(intra_op_num_threads=CPU_THREADS, inter_op_num_threads=1)
        self.ocr = MangaOcr()
        self.tagger = fugashi.Tagger()
        self.llm = Llama(model_path=str(MODEL), n_gpu_layers=-1, n_ctx=4096, n_threads=CPU_THREADS, verbose=False)
        self.glossary = glossary
        self.history = []
        self.cache = {}
        self.gloss_cache = {}

    def complete(self, user, temperature, max_tokens):
        out = self.llm.create_chat_completion(
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            temperature=temperature,
            top_p=0.8,
            max_tokens=max_tokens,
        )
        return out["choices"][0]["message"]["content"].strip()

    def prompt(self, ja, history):
        user = "历史翻译：" + "\n".join(history) + "\n" if history else ""
        return (
            user
            + "参考以下术语表（可为空，格式为src->dst #备注）：\n"
            + "\n".join(self.glossary)
            + "\n根据以上术语表的对应关系和备注，结合历史剧情和上下文，将下面的文本从日文翻译成简体中文：\n"
            + ja
        )

    def translate(self, ja):
        if ja in self.cache:
            return self.cache[ja]
        zh = self.complete(self.prompt(ja, self.history[-HISTORY_LINES:]), 0.3, 512)
        self.history.append(zh)
        self.cache[ja] = zh
        return zh

    def gloss(self, before, span, after):
        """选中的词在这句里的意思。Sakura 只会翻译、问它问题会把问题本身译掉,单译一个词又常照抄(素→素),
        所以给原句里的词加上【】整句翻译,取译文里括号中的那段(素→原本的自己)。括号丢了换［］再试,仍丢就单译这个词。"""
        key = (before, span, after)
        if key not in self.gloss_cache:
            for l, r in (("【", "】"), ("［", "］")):
                part = bracketed(self.complete(self.prompt(before + l + span + r + after, []), 0.1, 256),
                                 before + span + after)
                if part:
                    break
            else:
                part = self.complete(self.prompt(span, []), 0.1, 32)
            self.gloss_cache[key] = part
        return self.gloss_cache[key]


class App:
    def __init__(self):
        self.cfg = load_config()
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.translator = None
        self.auto = False
        self.last_sig = None
        self.pending_sig = None
        self.frames = deque(maxlen=FRAME_KEEP)

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg="#1e1e1e")
        self.root.attributes("-topmost", True)
        rect = find_game_rect(self.cfg["game_title"])
        if self.cfg["geometry"]:
            self.root.geometry(self.cfg["geometry"])
        elif rect:  # 首次打开放在游戏画面正下方,免得挡住对话框被截进去
            self.root.geometry(f"{rect[2]}x300+{rect[0]}+{rect[1] + rect[3] + 40}")
        else:
            self.root.geometry("1000x300")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.zh = tk.Label(self.root, text="", fg="#ffffff", bg="#1e1e1e", justify="left", anchor="nw",
                           font=("Noto Sans CJK SC", 18), wraplength=960)
        self.zh.pack(fill="both", expand=True, padx=12, pady=(10, 2))
        # 日文原文:带假名注音,词可点击
        self.base_font = tkfont.Font(family="Noto Sans CJK JP", size=14)
        self.ruby_font = tkfont.Font(family="Noto Sans CJK JP", size=8)
        self.tokens, self.tok_boxes, self.tok_text = [], [], []
        self.sel_start = None
        self.ja = tk.Canvas(self.root, bg="#1e1e1e", highlightthickness=0, height=40)
        self.ja.pack(fill="x", padx=12)
        self.ja.bind("<Motion>", self.on_ja_motion)
        self.ja.bind("<Leave>", lambda e: self.paint(None, None))
        self.ja.bind("<ButtonPress-1>", self.on_ja_press)
        self.ja.bind("<B1-Motion>", self.on_ja_drag)
        self.ja.bind("<ButtonRelease-1>", self.on_ja_release)
        word_bar = tk.Frame(self.root, bg="#1e1e1e")
        word_bar.pack(fill="x", padx=12, pady=(2, 0))
        self.word = tk.Label(word_bar, text="点日文里的词查原形、读音和词义；按住拖过几个词可查词组（结果会复制到剪贴板）",
                             fg="#7a7a7a", bg="#1e1e1e", anchor="w", font=("Noto Sans CJK SC", 11))
        self.word.pack(side="left", fill="x", expand=True)
        self.word_now = None
        self.word_head = ""
        tk.Button(word_bar, text="查词典", command=self.open_dict).pack(side="right")
        bar = tk.Frame(self.root, bg="#1e1e1e")
        bar.pack(fill="x", padx=12, pady=6)
        key = self.cfg["trigger_key"].upper()
        tk.Button(bar, text=f"翻译（{key} 键 / 鼠标侧键）", command=self.trigger).pack(side="left")
        tk.Button(bar, text="重新框选对话框", command=self.calibrate).pack(side="left", padx=6)
        self.auto_btn = tk.Button(bar, text="自动：关", command=self.toggle_auto)
        self.auto_btn.pack(side="left")
        self.status = tk.Label(bar, text="正在加载模型…", fg="#c8a24a", bg="#1e1e1e")
        self.status.pack(side="right")
        self.root.bind("<Configure>", lambda e: self.rewrap())

        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.sampler, daemon=True).start()
        self.listeners = [mouse.Listener(on_click=self.on_click), keyboard.Listener(on_press=self.on_press)]
        for listener in self.listeners:
            listener.start()
        self.root.after(50, self.poll_results)

    def rewrap(self):
        w = max(200, self.root.winfo_width() - 40)
        if w != self.zh.cget("wraplength"):
            self.zh.configure(wraplength=w)
            self.render_ja()

    def render_ja(self):
        """逐词排版:汉字上方小字注平假名,超宽换行;记下每个词的位置,供点选和拖选。"""
        c = self.ja
        c.delete("all")
        self.tok_boxes, self.tok_text = [], []
        width = max(200, c.winfo_width())
        rh, bh = self.ruby_font.metrics("linespace"), self.base_font.metrics("linespace")
        x = y = 0
        for tok in self.tokens:
            segs = ruby_segments(tok.surface, tok.reading) if tok.reading else [(tok.surface, None)]
            # 注音比汉字宽时把这一段撑宽(同网页 ruby),否则行首的「唇/くちびる」左半截伸出窗口被切掉,相邻注音也会叠在一起
            slots = [max(self.base_font.measure(seg), self.ruby_font.measure(ruby) if ruby else 0) for seg, ruby in segs]
            w = sum(slots)
            if x + w > width and x > 0:
                x, y = 0, y + rh + bh
            items, sx = [], x
            for (seg, ruby), slot in zip(segs, slots):
                items.append(c.create_text(sx + slot / 2, y + rh, text=seg, anchor="n", font=self.base_font,
                                           fill="#d0d0d0"))
                if ruby:
                    c.create_text(sx + slot / 2, y, text=ruby, anchor="n", font=self.ruby_font, fill="#c8a24a")
                sx += slot
            self.tok_text.append(items)
            self.tok_boxes.append((x, y, x + w, y + rh + bh))
            x += w
        c.configure(height=y + rh + bh + 4)

    def token_at(self, x, y):
        for i, (x0, y0, x1, y1) in enumerate(self.tok_boxes):
            if x0 <= x < x1 and y0 <= y < y1 and self.tokens[i].pos not in ("補助記号", "空白"):
                return i
        return None

    def paint(self, lo, hi):
        """lo..hi 之间的词标成蓝色(悬停或拖选中),其余恢复。"""
        for i, items in enumerate(self.tok_text):
            for item in items:
                self.ja.itemconfigure(item, fill="#6fc3ff" if lo is not None and lo <= i <= hi else "#d0d0d0")
        self.ja.configure(cursor="hand2" if lo is not None else "")

    def on_ja_motion(self, e):
        i = self.token_at(e.x, e.y)
        self.paint(i, i)

    def on_ja_press(self, e):
        self.sel_start = self.token_at(e.x, e.y)

    def on_ja_drag(self, e):
        i = self.token_at(e.x, e.y)
        if self.sel_start is not None and i is not None:
            self.paint(min(self.sel_start, i), max(self.sel_start, i))

    def on_ja_release(self, e):
        i = self.token_at(e.x, e.y)
        if self.sel_start is None or i is None:
            return
        lo, hi = min(self.sel_start, i), max(self.sel_start, i)
        self.sel_start = None
        self.lookup(lo, hi)

    def lookup(self, lo, hi):
        """单个词查原形;拖选的多个词按词组查:前面的词照原样,最后一个词换成原形,如 気/に/かけ → 気にかける。"""
        toks = self.tokens[lo:hi + 1]
        last = toks[-1]
        word = "".join(t.surface for t in toks[:-1]) + last.base
        reading = "".join(t.reading or jaconv.kata2hira(t.surface) for t in toks[:-1]) \
            + (last.base_reading or jaconv.kata2hira(last.base))
        self.word_now = word
        self.root.clipboard_clear()
        self.root.clipboard_append(word)
        reading = f"（{reading}）" if reading != word else ""
        self.word_head = f"{word}{reading}  {last.pos if len(toks) == 1 else '词组'}"
        self.word.configure(text=f"{self.word_head}  →  查询中…", fg="#ffffff")
        surfaces = [t.surface for t in self.tokens]
        self.jobs.put(("word", ("".join(surfaces[:lo]), "".join(surfaces[lo:hi + 1]), "".join(surfaces[hi + 1:]))))

    def open_dict(self):
        if self.word_now:
            webbrowser.open(self.cfg["dict_url"].format(word=quote(self.word_now)))

    def trigger(self):
        self.jobs.put(("line", time.time()))

    def on_click(self, x, y, button, pressed):
        if not pressed or button.name not in self.cfg["trigger_buttons"]:
            return
        rect = find_game_rect(self.cfg["game_title"])
        if rect and rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]:
            self.trigger()

    def on_press(self, key):
        name = getattr(key, "char", None) or getattr(key, "name", None)
        if not name or name.lower() != self.cfg["trigger_key"].lower():
            return
        if self.game_active() or active_window_title() == APP_TITLE:  # 在别的软件里打字时不触发
            self.trigger()

    def game_active(self):
        game = self.cfg["game_title"]
        return bool(game) and game in active_window_title()

    def grab_dialogue(self):
        rect = find_game_rect(self.cfg["game_title"])
        if rect is None:
            return None
        return ImageGrab.grab(bbox=region_bbox(rect, self.cfg["region"]), xdisplay=os.environ.get("DISPLAY", ":0"))

    def sampler(self):
        """游戏在前台时给台词区留底;触发时用按键前一刻的画面,按键本身让游戏藏起台词也不影响。"""
        while True:
            t = time.time()
            if self.game_active():
                img = self.grab_dialogue()
                if img is not None:
                    self.frames.append((t, img))
            time.sleep(SAMPLE_INTERVAL)

    def frame_before(self, t):
        for ts, img in reversed(self.frames):
            if ts <= t:
                return img if t - ts < 1.0 else self.grab_dialogue()
        return self.grab_dialogue()

    def worker(self):
        try:
            self.translator = Translator(self.cfg["glossary"])
        except Exception as e:
            self.results.put(("status", f"模型加载失败：{e}"))
            return
        self.results.put(("status", "就绪"))
        while True:
            kind, arg = self.jobs.get()
            try:
                self.run_job(kind, arg)
            except Exception as e:  # 一次出错不能让后台线程死掉,否则之后再按 T 都没反应
                traceback.print_exc()
                self.results.put(("status", f"出错了：{type(e).__name__}，可再按一次"))

    def run_job(self, kind, arg):
        if kind == "word":
            self.results.put(("word", self.translator.gloss(*arg)))
            return
        img = self.frame_before(arg)
        if img is None:
            self.results.put(("status", self.no_game_hint()))
            return
        self.results.put(("status", "识别中…"))
        img.save(HERE / "last_capture.png")  # 只留最新一张,认错字或出错时拿来查
        ja = ocr_dialogue(self.translator.det, self.translator.ocr, img).strip()
        (HERE / "last_capture.txt").write_text(ja, encoding="utf-8")
        if not ja:
            self.results.put(("status", "对话框里没识别到文字"))
            return
        self.results.put(("ja", tokenize(self.translator.tagger, ja, self.cfg["readings"])))
        self.results.put(("status", "翻译中…"))
        self.results.put(("zh", self.translator.translate(ja)))
        self.results.put(("status", "就绪"))

    def poll_results(self):
        while not self.results.empty():
            kind, value = self.results.get()
            if kind == "status":
                self.status.configure(text=value)
            elif kind == "ja":
                self.tokens = value
                self.render_ja()
                self.zh.configure(text="")
            elif kind == "word":
                self.word.configure(text=f"{self.word_head}  →  {value}")
            else:
                self.zh.configure(text=value)
        self.root.after(50, self.poll_results)

    def toggle_auto(self):
        self.auto = not self.auto
        self.auto_btn.configure(text="自动：开" if self.auto else "自动：关")
        if self.auto:
            self.last_sig = None
            self.root.after(AUTO_POLL_MS, self.auto_tick)

    def auto_tick(self):
        """自动模式:对话框画面连续两次采样一致且与上次翻译时不同,就触发翻译。"""
        if not self.auto:
            return
        img = self.frames[-1][1] if self.translator and self.frames else None
        if img is not None:
            sig = ImageOps.grayscale(img).resize((64, 16)).tobytes()
            if sig == self.pending_sig and sig != self.last_sig:
                self.last_sig = sig
                self.trigger()
            self.pending_sig = sig
        self.root.after(AUTO_POLL_MS, self.auto_tick)

    def no_game_hint(self):
        return "没找到游戏窗口" if self.cfg["game_title"] else "先在 config.json 里填 game_title(游戏窗口标题的一部分)"

    def calibrate(self):
        rect = find_game_rect(self.cfg["game_title"])
        if rect is None:
            self.status.configure(text=self.no_game_hint())
            return
        x, y, w, h = rect
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.attributes("-topmost", True)
        top.attributes("-alpha", 0.35)
        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(w // 2, h // 2, text="按住左键拖动，框住对话框（含人名），松开完成；Esc 取消",
                           fill="white", font=("Noto Sans CJK SC", 20))
        state = {}

        def press(e):
            state["start"] = (e.x, e.y)
            state["box"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff4040", width=3)

        def drag(e):
            sx, sy = state["start"]
            canvas.coords(state["box"], sx, sy, e.x, e.y)

        def release(e):
            sx, sy = state["start"]
            l, r = sorted((sx, e.x))
            t, b = sorted((sy, e.y))
            top.destroy()
            if r - l < 20 or b - t < 10:
                self.status.configure(text="框太小，已取消")
                return
            self.cfg["region"] = [l / w, t / h, r / w, b / h]
            save_config(self.cfg)
            self.status.configure(text="对话框位置已保存")

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: top.destroy())
        top.focus_force()

    def quit(self):
        self.cfg["geometry"] = self.root.geometry()
        save_config(self.cfg)
        for listener in self.listeners:
            listener.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
