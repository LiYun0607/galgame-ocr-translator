"""生词本:查过的词记下来,按间隔重复复习。"""
import datetime
import json
import os
import re
import tkinter as tk
from collections import deque

INTERVALS = [1, 3, 7, 14, 30, 60]  # 连续记得第 n 次后隔多少天再考,用完后都按最后一档
BG = "#1e1e1e"
WIDTH, HEIGHT = 780, 440  # 复习窗最小尺寸,内容多时自动加高


def today():
    return datetime.date.today()


def sentence_of(before, span, after):
    """一段文字里 span 所在的那句,复习时当例句;一屏累积多句时不把整段都存下来。"""
    head = re.split("[。！？]", before)[-1]
    tail = re.match("[^。！？]*[。！？]*[」』）]?", after).group()
    return head + span + tail


class Vocab:
    def __init__(self, path):
        self.path = path
        self.words = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self):
        # 先写临时文件再替换,写到一半崩溃也不会弄坏已有的生词本
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.words, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, word, reading, pos, span, sentence, meaning, zh):
        """查词时调用。同一句里再点一次不算数;换了句子又查,说明忘了,从头复习。"""
        old = self.words.get(word)
        if old and old["sentence"] == sentence:
            return
        self.words[word] = {"reading": reading, "pos": pos, "span": span, "sentence": sentence, "meaning": meaning,
                            "zh": zh, "added": old["added"] if old else today().isoformat(),
                            "count": old["count"] + 1 if old else 1, "box": 0, "due": today().isoformat()}
        self.save()

    def due(self):
        """今天该复习的词,到期早的在前。"""
        now = today().isoformat()
        return sorted((w for w, e in self.words.items() if e["due"] <= now),
                      key=lambda w: (self.words[w]["due"], self.words[w]["added"]))

    def answer(self, word, remembered):
        e = self.words[word]
        if remembered:
            days = INTERVALS[min(e["box"], len(INTERVALS) - 1)]
            e["box"] += 1
            e["due"] = (today() + datetime.timedelta(days=days)).isoformat()
        else:
            e["box"] = 0
            e["due"] = today().isoformat()
        self.save()

    def remove(self, word):
        del self.words[word]
        self.save()


class ReviewWindow:
    """一次一张卡:先看词和例句,回想读音和意思,按空格看答案,再选记得或不记得。不记得的排到队尾,这一轮里再考。"""

    def __init__(self, root, vocab, on_change):
        self.vocab = vocab
        self.on_change = on_change
        self.queue = deque(vocab.due())
        self.word = None
        self.revealed = False

        self.top = top = tk.Toplevel(root)
        top.title("生词复习")
        top.configure(bg=BG)
        top.attributes("-topmost", True)  # 主窗口也是置顶的,不置顶会被它盖住
        top.geometry(f"{WIDTH}x{HEIGHT}")
        top.minsize(WIDTH, HEIGHT)
        self.left = tk.Label(top, fg="#7a7a7a", bg=BG, anchor="e", font=("Noto Sans CJK SC", 11))
        self.left.pack(fill="x", padx=16, pady=(10, 0))
        self.head = tk.Label(top, fg="#ffffff", bg=BG, font=("Noto Sans CJK JP", 30))
        self.head.pack(pady=(4, 10))
        self.sentence = self.text_box("#d0d0d0", ("Noto Sans CJK JP", 16))
        self.sentence.tag_configure("hit", foreground="#6fc3ff")
        self.sentence.pack(fill="x", padx=16)
        self.answer = self.text_box("#ffffff", ("Noto Sans CJK SC", 15))
        self.answer.pack(fill="both", expand=True, padx=16, pady=(6, 0))
        bar = tk.Frame(top, bg=BG)
        bar.pack(fill="x", padx=16, pady=10)
        self.show_btn = tk.Button(bar, text="看答案（空格）", command=self.reveal)
        self.no_btn = tk.Button(bar, text="不记得（1）", command=lambda: self.grade(False))
        self.yes_btn = tk.Button(bar, text="记得（2）", command=lambda: self.grade(True))
        self.drop_btn = tk.Button(bar, text="不用背（Delete）", command=self.drop)
        self.drop_btn.pack(side="right")
        for key in ("<space>", "<Return>", "<KP_Enter>"):
            top.bind(key, lambda e: self.reveal())
        for key in ("1", "<KP_1>", "<KP_End>"):  # 小键盘的键名和主键盘不同,NumLock 关着时又是另一个
            top.bind(key, lambda e: self.grade(False))
        for key in ("2", "<KP_2>", "<KP_Down>"):
            top.bind(key, lambda e: self.grade(True))
        top.bind("<Delete>", lambda e: self.drop())
        top.bind("<Escape>", lambda e: top.destroy())
        top.focus_force()
        self.next_card()

    def text_box(self, fg, font):
        return tk.Text(self.top, height=1, width=1, wrap="char", bg=BG, fg=fg, relief="flat", highlightthickness=0,
                       font=font, takefocus=0, cursor="arrow", state="disabled")

    def fill(self, box, text, span=""):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        if span and span in text:
            i = text.index(span)
            box.tag_add("hit", f"1.0+{i}c", f"1.0+{i + len(span)}c")
        box.configure(state="disabled")

    def fit(self):
        """文字框的行数和窗口高度跟着内容走,否则长句和整段译文显示不全。"""
        top = self.top
        top.update_idletasks()
        for box in (self.sentence, self.answer):
            lines = box.tk.call(box._w, "count", "-update", "-displaylines", "1.0", "end")
            box.configure(height=max(1, int(lines)))
        top.update_idletasks()
        top.geometry(f"{max(top.winfo_width(), WIDTH)}x{max(top.winfo_reqheight(), HEIGHT)}")

    def next_card(self):
        self.revealed = False
        self.no_btn.pack_forget()
        self.yes_btn.pack_forget()
        self.fill(self.answer, "")
        self.fill(self.sentence, "")
        if not self.queue:
            self.word = None
            self.left.configure(text="")
            self.head.configure(text="今天的词复习完了")
            self.show_btn.pack_forget()
            self.drop_btn.pack_forget()
            self.fit()
            return
        self.word = self.queue.popleft()
        e = self.vocab.words[self.word]
        self.left.configure(text=f"还剩 {len(self.queue) + 1} 个")
        self.head.configure(text=self.word)
        self.fill(self.sentence, e["sentence"], e["span"])
        self.show_btn.pack(side="left")
        self.fit()

    def reveal(self):
        if self.word is None or self.revealed:
            return
        self.revealed = True
        e = self.vocab.words[self.word]
        self.fill(self.answer, f"{e['reading']}  {e['pos']}\n{e['meaning']}\n\n{e['zh']}")
        self.show_btn.pack_forget()
        self.no_btn.pack(side="left")
        self.yes_btn.pack(side="left", padx=6)
        self.fit()

    def grade(self, remembered):
        if self.word is None:
            return
        if not self.revealed:  # 没看答案就按了打分键:先给答案,再按一次才算数
            self.reveal()
            return
        self.vocab.answer(self.word, remembered)
        if not remembered:
            self.queue.append(self.word)
        self.on_change()
        self.next_card()

    def drop(self):
        if self.word is None:
            return
        self.vocab.remove(self.word)
        self.on_change()
        self.next_card()
