#!/usr/bin/env python3
"""
e2player - player de TV ao estilo MAG (Infomir) para boxes Enigma2.

  * grupos = bouquets da box (+ Favoritos e Todos os canais)
  * lista de canais com programa atual e barra de progresso
  * pré-visualização do vídeo ao lado da lista, como no portal MAG
  * barra de informação (agora / a seguir) ao mudar de canal
  * guia EPG completo por canal
  * picons da box, favoritos, zapping por número, canal anterior
  * o e2proxy corre embutido: todos os canais passam pela correção da PMT

Requisitos (Windows ou Linux):
  - Python 3.8+ com Tkinter  (no Linux: sudo apt install python3-tk)
  - VLC instalado (no Windows, VLC de 64 bits se o Python for de 64 bits)
  - pip install python-vlc
  - opcional: pip install pillow   (picons com melhor qualidade)
  - e2proxy.py na mesma pasta

Uso:
  python3 e2player.py --box 192.168.1.65 [--user root --password xxx]
  (a configuração fica guardada em ~/.e2player.json; nas vezes seguintes
   basta  python3 e2player.py)

Teclas (ecrã inteiro):
  Cima / Baixo, PgUp / PgDn   canal seguinte / anterior
  OK (Enter)                  abrir a lista de canais
  Esquerda / Direita, + / -   volume
  0-9                         zapping por número
  Backspace                   canal anterior
  I  info    E / G  guia EPG    F  favorito    M  mute
  A  faixa de áudio    S  legendas    F11  janela/ecrã inteiro
  Ctrl+Q  sair

Teclas (lista):
  Cima / Baixo, PgUp / PgDn, Home / End   mover
  Esquerda / Direita          grupos <-> canais
  OK                          1.º OK reproduz na pré-visualização,
                              2.º OK passa a ecrã inteiro
  E / G  guia do canal    F  favorito    Esc  voltar ao vídeo
"""

import argparse
import base64
import io
import json
import logging
import math
import os
import queue
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2proxy  # noqa: E402
from e2proxy import OpenWebif, normref  # noqa: E402

try:
    import tkinter as tk
    import tkinter.font as tkfont
except ImportError:          # permite usar o Model sem interface (testes)
    tk = None

log = logging.getLogger("e2player")

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".e2player.json")

DIAS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
MESES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
         "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

# cores
BG = "#0b0e14"
PANEL = "#131924"
PANEL2 = "#1a2230"
SEL = "#2563c9"
SEL_DIM = "#2a3446"
TEXT = "#e9edf3"
DIM = "#8b95a7"
ACCENT = "#f5a524"
PLAY = "#4cc38a"
BAR_BG = "#2a3242"
FAV = "#f5c542"


def hhmm(t):
    return time.strftime("%H:%M", time.localtime(t))


def day_label(t):
    lt = time.localtime(t)
    return "%s %02d/%02d" % (DIAS[lt.tm_wday], lt.tm_mday, lt.tm_mon)


def clock_label(t=None):
    lt = time.localtime(t)
    return "%s %d %s  %02d:%02d" % (DIAS[lt.tm_wday], lt.tm_mday,
                                    MESES[lt.tm_mon - 1], lt.tm_hour, lt.tm_min)


# ----------------------------------------------------------------------------
# Modelo (sem interface)
# ----------------------------------------------------------------------------

class Channel:
    __slots__ = ("num", "name", "ref", "key", "group")

    def __init__(self, num, name, ref, group):
        self.num, self.name, self.ref, self.group = num, name, ref, group
        self.key = normref(ref)


class Group:
    def __init__(self, name, bref=None, channels=None, kind="bouquet"):
        self.name, self.bref, self.kind = name, bref, kind
        self.channels = channels if channels is not None else []


class Model:
    def __init__(self, favorites=()):
        self.bouquets = []
        self.groups = []
        self.by_num = {}
        self.by_key = {}
        self.favorites = list(favorites)
        self.nownext = {}          # normref -> [eventos]
        self.epg_time = {}         # bref -> última atualização

    def load(self, bouquets):
        num = 0
        self.bouquets, self.by_num, self.by_key = [], {}, {}
        for name, bref, chans in bouquets:
            g = Group(name, bref)
            for ref, cname in chans:
                num += 1
                ch = Channel(num, cname or ref, ref, g)
                g.channels.append(ch)
                self.by_num[num] = ch
                self.by_key.setdefault(ch.key, ch)
            self.bouquets.append(g)
        self.rebuild_groups()

    def rebuild_groups(self):
        favs = [self.by_key[k] for k in self.favorites if k in self.by_key]
        groups = []
        if favs:
            groups.append(Group("★ Favoritos", None, favs, "fav"))
        groups += self.bouquets
        allch = [ch for g in self.bouquets for ch in g.channels]
        if allch:
            groups.append(Group("Todos os canais", None, allch, "all"))
        self.groups = groups

    def is_fav(self, ch):
        return ch.key in self.favorites

    def toggle_fav(self, ch):
        if ch.key in self.favorites:
            self.favorites.remove(ch.key)
            fav = False
        else:
            self.favorites.append(ch.key)
            fav = True
        self.rebuild_groups()
        return fav

    def merge_epg(self, bref, data):
        self.nownext.update(data)
        self.epg_time[bref] = time.time()

    def now_next(self, ch, t=None):
        t = time.time() if t is None else t
        now = nxt = None
        for ev in self.nownext.get(ch.key, ()):
            if ev["start"] <= t < ev["start"] + ev["dur"]:
                now = ev
            elif ev["start"] > t and nxt is None:
                nxt = ev
        return now, nxt

    def stale_brefs(self, channels, t=None, min_age=60):
        """bouquets cujo EPG já não tem evento atual para algum canal."""
        t = time.time() if t is None else t
        out = set()
        for ch in channels:
            b = ch.group.bref
            if not b or b in out or t - self.epg_time.get(b, 0) < min_age:
                continue
            evs = self.nownext.get(ch.key)
            if evs and self.now_next(ch, t)[0] is None:
                out.add(b)
        return out

    def neighbour(self, group, ch, step):
        chs = group.channels
        if not chs:
            return None
        try:
            i = chs.index(ch)
        except ValueError:
            return chs[0]
        return chs[(i + step) % len(chs)]

    def find_group_index(self, ch, prefer=None):
        if prefer is not None and prefer in self.groups and ch in prefer.channels:
            return self.groups.index(prefer)
        for i, g in enumerate(self.groups):
            if g is ch.group:
                return i
        return 0


def progress(ev, t=None):
    if not ev or ev["dur"] <= 0:
        return 0.0
    t = time.time() if t is None else t
    return max(0.0, min(1.0, (t - ev["start"]) / ev["dur"]))


def elide(font, text, maxw):
    if maxw <= 0:
        return ""
    if font.measure(text) <= maxw:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------

class App:
    def __init__(self, root, cfg, owif, proxy_port):
        import vlc
        self.vlcmod = vlc
        self.root = root
        self.cfg = cfg
        self.owif = owif
        self.proxy_port = proxy_port
        self.model = Model(cfg.get("favorites", []))

        self.mode = "list"          # "list" | "full"
        self.focus = "groups"       # "groups" | "channels"
        self.gi = self.ci = 0
        self.gtop = self.ctop = 0
        self.playing = None
        self.play_group = None
        self.prev = None
        self.status = "A carregar canais da box…"
        self.volume = int(cfg.get("volume", 80))
        self.retry = 0

        self.epg_ch = None          # canal do guia aberto
        self.epg_events = None
        self.epg_sel = 0
        self.epg_top = 0

        self.numbuf = ""
        self.num_job = None
        self.infobar_job = None
        self.toast_job = None
        self.resize_job = None
        self.picons = {}

        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=4)

        self._build_ui()
        self._init_vlc()
        root.bind("<Key>", self.on_key)
        root.bind("<Configure>", self.on_resize)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(50, self._poll)
        self.root.after(1000, self._tick)
        self.load_channels()

    # ------------------------------------------------------------ infra
    def run_bg(self, fn, cb, *args):
        def task():
            try:
                res, err = fn(*args), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            self.q.put((cb, res, err))
        self.pool.submit(task)

    def _poll(self):
        try:
            while True:
                cb, res, err = self.q.get_nowait()
                try:
                    cb(res, err)
                except Exception:
                    log.exception("erro no callback")
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _tick(self):
        self.draw_header()
        if self.infobar.winfo_ismapped():
            self.draw_infobar()
        if int(time.time()) % 30 == 0:
            self.redraw()
            self._refresh_stale_epg()
        self.root.after(1000, self._tick)

    def save_cfg(self):
        self.cfg["favorites"] = self.model.favorites
        self.cfg["volume"] = self.volume
        if self.playing:
            self.cfg["last"] = self.playing.ref
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except OSError as e:
            log.warning("não foi possível guardar a configuração: %s", e)

    def quit(self):
        self.save_cfg()
        try:
            self.player.stop()
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        r = self.root
        r.configure(bg=BG)
        self.video = tk.Frame(r, bg="black", highlightthickness=0)
        mk = lambda bg: tk.Canvas(r, bg=bg, highlightthickness=0, bd=0)  # noqa: E731
        self.header = mk(PANEL)
        self.gcanvas = mk(PANEL)
        self.ccanvas = mk(PANEL)
        self.info = mk(PANEL)
        self.infobar = mk(PANEL)
        self.toast = tk.Label(r, bg=PANEL2, fg=TEXT, padx=16, pady=8)
        self.numbox = tk.Label(r, bg=SEL, fg=TEXT, padx=18, pady=6)

        for c, kind in ((self.gcanvas, "groups"), (self.ccanvas, "channels")):
            c.bind("<Button-1>", lambda e, k=kind: self.on_click(k, e, False))
            c.bind("<Double-Button-1>", lambda e, k=kind: self.on_click(k, e, True))
            c.bind("<MouseWheel>", lambda e, k=kind: self.on_wheel(k, -1 if e.delta > 0 else 1))
            c.bind("<Button-4>", lambda e, k=kind: self.on_wheel(k, -1))
            c.bind("<Button-5>", lambda e, k=kind: self.on_wheel(k, 1))
        self.video.bind("<Double-Button-1>", lambda e: self.toggle_mode())
        self.infobar.bind("<Double-Button-1>", lambda e: self.toggle_mode())
        self._make_fonts()
        self.layout()

    def _make_fonts(self):
        h = max(self.root.winfo_height(), 600)
        base = max(11, h // 50)
        fam = "Segoe UI" if sys.platform.startswith("win") else "DejaVu Sans"
        self.f_small = tkfont.Font(family=fam, size=-int(base * 0.85))
        self.f_norm = tkfont.Font(family=fam, size=-base)
        self.f_bold = tkfont.Font(family=fam, size=-base, weight="bold")
        self.f_big = tkfont.Font(family=fam, size=-int(base * 1.35), weight="bold")
        self.f_huge = tkfont.Font(family=fam, size=-int(base * 2.2), weight="bold")
        self.toast.configure(font=self.f_bold)
        self.numbox.configure(font=self.f_huge)
        self.picons.clear()   # tamanho depende do ecrã

    def on_resize(self, e):
        if e.widget is not self.root:
            return
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(150, self._do_resize)

    def _do_resize(self):
        self.resize_job = None
        self._make_fonts()
        self.layout()

    def layout(self):
        W = max(self.root.winfo_width(), 640)
        H = max(self.root.winfo_height(), 360)
        hh = int(H * 0.07)
        if self.mode == "full":
            for w in (self.header, self.gcanvas, self.ccanvas, self.info):
                w.place_forget()
            self.video.place(x=0, y=0, width=W, height=H)
            if self.infobar.winfo_ismapped():
                self._place_infobar()
        else:
            self.infobar.place_forget()
            self.header.place(x=0, y=0, width=W, height=hh)
            top = hh + int(H * 0.015)
            bottom = H - int(H * 0.025)
            gx, gw = int(W * 0.015), int(W * 0.19)
            cx, cw = gx + gw + int(W * 0.01), int(W * 0.37)
            vx = cx + cw + int(W * 0.01)
            vw = W - vx - int(W * 0.015)
            vh = int(vw * 9 / 16)
            self.gcanvas.place(x=gx, y=top, width=gw, height=bottom - top)
            self.ccanvas.place(x=cx, y=top, width=cw, height=bottom - top)
            self.video.place(x=vx, y=top, width=vw, height=vh)
            iy = top + vh + int(H * 0.015)
            self.info.place(x=vx, y=iy, width=vw, height=bottom - iy)
        self.root.update_idletasks()
        self.redraw()

    def _place_infobar(self):
        W, H = self.root.winfo_width(), self.root.winfo_height()
        self.infobar.place(x=int(W * 0.03), y=int(H * 0.76),
                           width=int(W * 0.94), height=int(H * 0.21))
        self.infobar.lift()

    def toggle_mode(self):
        if self.mode == "full":
            self.open_list()
        elif self.playing:
            self.go_full()

    def open_list(self):
        self.mode = "list"
        if self.playing and self.model.groups:
            g = self.model.find_group_index(self.playing, self.play_group)
            self.select_group(g)
            chs = self.model.groups[g].channels
            if self.playing in chs:
                self.ci = chs.index(self.playing)
            self.focus = "channels"
        self.layout()

    def go_full(self):
        self.mode = "full"
        self.epg_ch = None
        self.layout()
        self.show_infobar()

    # ------------------------------------------------------------ desenho
    def redraw(self):
        if self.mode == "list":
            self.draw_header()
            self.draw_groups()
            if self.epg_ch:
                self.draw_epg()
            else:
                self.draw_channels()
            self.draw_info()
        elif self.infobar.winfo_ismapped():
            self.draw_infobar()

    def row_h(self):
        return int(self.f_norm.metrics("linespace") * 2.7)

    def draw_header(self):
        if self.mode != "list":
            return
        c = self.header
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        pad = int(w * 0.015)
        c.create_text(pad, h // 2, anchor="w", text="TV", font=self.f_big, fill=ACCENT)
        gname = ""
        if self.model.groups:
            gname = self.model.groups[self.gi].name
        if self.epg_ch:
            gname = "Guia de programas · " + self.epg_ch.name
        c.create_text(pad + self.f_big.measure("TV  "), h // 2, anchor="w",
                      text=gname, font=self.f_big, fill=TEXT)
        c.create_text(w - pad, h // 2, anchor="e", text=clock_label(),
                      font=self.f_bold, fill=TEXT)

    def _scroll(self, sel, top, rows, n):
        if sel < top:
            top = sel
        elif sel >= top + rows:
            top = sel - rows + 1
        return max(0, min(top, max(0, n - rows)))

    def _scrollbar(self, c, w, h, top, rows, n):
        if n <= rows:
            return
        bh = max(20, int(h * rows / n))
        by = int((h - bh) * top / max(1, n - rows))
        c.create_rectangle(w - 4, by, w, by + bh, fill=SEL_DIM, outline="")

    def draw_groups(self):
        c = self.gcanvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        groups = self.model.groups
        rh = int(self.f_norm.metrics("linespace") * 2.0)
        rows = max(1, h // rh)
        self.gtop = self._scroll(self.gi, self.gtop, rows, len(groups))
        pad = 12
        for i in range(self.gtop, min(len(groups), self.gtop + rows)):
            g = groups[i]
            y = (i - self.gtop) * rh
            if i == self.gi:
                fill = SEL if self.focus == "groups" and not self.epg_ch else SEL_DIM
                c.create_rectangle(0, y + 2, w - 6, y + rh - 2, fill=fill, outline="")
            cnt = str(len(g.channels))
            cw = self.f_small.measure(cnt)
            c.create_text(w - 12, y + rh // 2, anchor="e", text=cnt,
                          font=self.f_small, fill=DIM if i != self.gi else TEXT)
            c.create_text(pad, y + rh // 2, anchor="w", font=self.f_norm, fill=TEXT,
                          text=elide(self.f_norm, g.name, w - pad - cw - 30))
        self._scrollbar(c, w, h, self.gtop, rows, len(groups))

    def draw_channels(self):
        c = self.ccanvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not self.model.groups:
            c.create_text(w // 2, h // 2, text=self.status, font=self.f_norm,
                          fill=DIM, width=w - 40, justify="center")
            return
        chs = self.model.groups[self.gi].channels
        rh = self.row_h()
        rows = max(1, h // rh)
        self.ci = max(0, min(self.ci, len(chs) - 1))
        self.ctop = self._scroll(self.ci, self.ctop, rows, len(chs))
        pad = 12
        numw = self.f_bold.measure("0000")
        barw = int(w * 0.16)
        t = time.time()
        for i in range(self.ctop, min(len(chs), self.ctop + rows)):
            ch = chs[i]
            y = (i - self.ctop) * rh
            sel = i == self.ci
            if sel:
                fill = SEL if self.focus == "channels" else SEL_DIM
                c.create_rectangle(0, y + 2, w - 6, y + rh - 2, fill=fill, outline="")
            y1, y2 = y + int(rh * 0.33), y + int(rh * 0.70)
            c.create_text(pad + numw, y1, anchor="e", text=str(ch.num),
                          font=self.f_bold, fill=TEXT if sel else DIM)
            x = pad + numw + 14
            right = w - 16
            if ch is self.playing:
                c.create_text(right, y1, anchor="e", text="▶", font=self.f_norm, fill=PLAY)
                right -= self.f_norm.measure("▶ ")
            if self.model.is_fav(ch):
                c.create_text(right, y1, anchor="e", text="★", font=self.f_norm, fill=FAV)
                right -= self.f_norm.measure("★ ")
            c.create_text(x, y1, anchor="w", font=self.f_bold, fill=TEXT,
                          text=elide(self.f_bold, ch.name, right - x))
            now, _ = self.model.now_next(ch, t)
            if now:
                c.create_text(x, y2, anchor="w", font=self.f_small,
                              fill=TEXT if sel else DIM,
                              text=elide(self.f_small, now["title"], w - x - barw - 30))
                bx = w - 16 - barw
                by = y2 - 2
                c.create_rectangle(bx, by, bx + barw, by + 5, fill=BAR_BG, outline="")
                c.create_rectangle(bx, by, bx + int(barw * progress(now, t)), by + 5,
                                   fill=ACCENT, outline="")
        self._scrollbar(c, w, h, self.ctop, rows, len(chs))

    def cursor_channel(self):
        if not self.model.groups:
            return None
        chs = self.model.groups[self.gi].channels
        if not chs:
            return None
        return chs[max(0, min(self.ci, len(chs) - 1))]

    def _picon_img(self, ch, box_h):
        p = self.picons.get(ch.key)
        if p is None:
            self.picons[ch.key] = "loading"
            self.run_bg(self.owif.picon, lambda data, err, k=ch.key, bh=box_h:
                        self._picon_loaded(k, data, bh), ch.ref)
            return None
        return p if p not in ("loading", "none") else None

    def _picon_loaded(self, key, data, box_h):
        if not data:
            self.picons[key] = "none"
            return
        img = None
        try:
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(data)).convert("RGBA")
            im.thumbnail((box_h * 2, box_h))
            img = ImageTk.PhotoImage(im)
        except Exception:
            try:
                img = tk.PhotoImage(data=base64.b64encode(data))
                f = max(1, math.ceil(img.height() / box_h),
                        math.ceil(img.width() / (box_h * 2)))
                if f > 1:
                    img = img.subsample(f, f)
            except Exception:
                img = None
        self.picons[key] = img or "none"
        self.redraw()

    def draw_info(self):
        c = self.info
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        pad = 16
        if self.epg_ch:
            return self._draw_epg_info(c, w, h, pad)
        ch = self.cursor_channel()
        if not ch:
            return
        ph = int(self.f_big.metrics("linespace") * 1.8)
        x = pad
        img = self._picon_img(ch, ph)
        if img:
            c.create_image(pad, pad, anchor="nw", image=img)
            x = pad + img.width() + 14
        c.create_text(x, pad, anchor="nw", text=elide(self.f_big, ch.name, w - x - pad),
                      font=self.f_big, fill=TEXT)
        c.create_text(x, pad + self.f_big.metrics("linespace") + 2, anchor="nw",
                      text="%d · %s" % (ch.num, ch.group.name), font=self.f_small, fill=DIM)
        y = pad + ph + 14
        now, nxt = self.model.now_next(ch)
        if not now and not nxt:
            c.create_text(pad, y, anchor="nw", text="Sem informação de programa",
                          font=self.f_norm, fill=DIM)
            return
        if now:
            y = self._event_block(c, now, pad, y, w - 2 * pad, show_bar=True)
        if nxt:
            c.create_text(pad, y, anchor="nw", fill=DIM, font=self.f_small,
                          text="A seguir  %s  %s" % (hhmm(nxt["start"]), nxt["title"]),
                          width=w - 2 * pad)
            y += self.f_small.metrics("linespace") + 10
        if now and now["desc"]:
            c.create_text(pad, y, anchor="nw", text=now["desc"], font=self.f_small,
                          fill=DIM, width=w - 2 * pad)

    def _event_block(self, c, ev, x, y, width, show_bar):
        t = time.time()
        times = "%s – %s" % (hhmm(ev["start"]), hhmm(ev["start"] + ev["dur"]))
        c.create_text(x, y, anchor="nw", text=times, font=self.f_bold, fill=ACCENT)
        tx = x + self.f_bold.measure(times + "   ")
        c.create_text(tx, y, anchor="nw", font=self.f_bold, fill=TEXT,
                      text=elide(self.f_bold, ev["title"], width - (tx - x)))
        y += self.f_bold.metrics("linespace") + 8
        if show_bar and ev["start"] <= t:
            left = max(0, int((ev["start"] + ev["dur"] - t) / 60))
            lbl = "+%d min" % left
            lw = self.f_small.measure(lbl) + 10
            bw = width - lw
            c.create_rectangle(x, y + 3, x + bw, y + 9, fill=BAR_BG, outline="")
            c.create_rectangle(x, y + 3, x + int(bw * progress(ev, t)), y + 9,
                               fill=ACCENT, outline="")
            c.create_text(x + width, y + 6, anchor="e", text=lbl, font=self.f_small, fill=DIM)
            y += 22
        return y

    # ------------------------------------------------------------ infobar
    def show_infobar(self, secs=5):
        if self.mode != "full" or not self.playing:
            return
        self._place_infobar()
        self.root.update_idletasks()
        self.draw_infobar()
        if self.infobar_job:
            self.root.after_cancel(self.infobar_job)
        self.infobar_job = self.root.after(secs * 1000, self.hide_infobar)

    def hide_infobar(self):
        self.infobar_job = None
        self.infobar.place_forget()

    def draw_infobar(self):
        c = self.infobar
        c.delete("all")
        ch = self.playing
        if not ch:
            return
        w, h = c.winfo_width(), c.winfo_height()
        pad = int(h * 0.12)
        c.create_rectangle(0, 0, 6, h, fill=SEL, outline="")
        num = str(ch.num)
        c.create_text(pad + 6, pad, anchor="nw", text=num, font=self.f_huge, fill=ACCENT)
        x = pad + 6 + self.f_huge.measure(num) + 22
        ph = int(h * 0.42)
        img = self._picon_img(ch, ph)
        if img:
            c.create_image(x, pad, anchor="nw", image=img)
            x += img.width() + 18
        c.create_text(w - pad, pad, anchor="ne", text=clock_label(),
                      font=self.f_bold, fill=TEXT)
        c.create_text(x, pad, anchor="nw", font=self.f_big, fill=TEXT,
                      text=elide(self.f_big, ch.name,
                                 w - x - pad - self.f_bold.measure(clock_label()) - 20))
        c.create_text(x, pad + self.f_big.metrics("linespace") + 2, anchor="nw",
                      text=ch.group.name + ("   ★" if self.model.is_fav(ch) else ""),
                      font=self.f_small, fill=DIM)
        now, nxt = self.model.now_next(ch)
        y = pad + ph + 6
        ex = pad + 6
        if now:
            y = self._event_block(c, now, ex, y, w - ex - pad, show_bar=True)
        if nxt:
            c.create_text(ex, y, anchor="nw", fill=DIM, font=self.f_norm,
                          text=elide(self.f_norm, "A seguir   %s   %s"
                                     % (hhmm(nxt["start"]), nxt["title"]), w - ex - pad))
        if not now and not nxt:
            c.create_text(ex, y, anchor="nw", text="Sem informação de programa",
                          font=self.f_norm, fill=DIM)

    # ------------------------------------------------------------ EPG
    def open_epg(self, ch):
        if not ch:
            return
        self.mode = "list"
        self.epg_ch = ch
        self.epg_events = None
        self.epg_sel = self.epg_top = 0
        self.layout()
        self.run_bg(self.owif.epg_service, lambda evs, err, c=ch: self._epg_loaded(c, evs, err),
                    ch.ref)

    def _epg_loaded(self, ch, evs, err):
        if ch is not self.epg_ch:
            return
        if err:
            log.warning("EPG de %s falhou: %s", ch.name, err)
        t = time.time()
        self.epg_events = [e for e in (evs or []) if e["start"] + e["dur"] > t]
        self.epg_sel = 0
        self.redraw()

    def close_epg(self):
        self.epg_ch = None
        self.redraw()

    def draw_epg(self):
        c = self.ccanvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if self.epg_events is None:
            c.create_text(w // 2, h // 2, text="A carregar o guia…", font=self.f_norm, fill=DIM)
            return
        if not self.epg_events:
            c.create_text(w // 2, h // 2, text="Sem guia de programas para este canal",
                          font=self.f_norm, fill=DIM)
            return
        evs = self.epg_events
        rh = int(self.f_norm.metrics("linespace") * 2.0)
        rows = max(1, h // rh)
        self.epg_sel = max(0, min(self.epg_sel, len(evs) - 1))
        self.epg_top = self._scroll(self.epg_sel, self.epg_top, rows, len(evs))
        t = time.time()
        pad = 12
        dw = self.f_small.measure("Sáb 00/00  ")
        tw = self.f_bold.measure("00:00   ")
        last_day = None
        for i in range(self.epg_top, min(len(evs), self.epg_top + rows)):
            ev = evs[i]
            y = (i - self.epg_top) * rh
            if i == self.epg_sel:
                c.create_rectangle(0, y + 2, w - 6, y + rh - 2, fill=SEL, outline="")
            cy = y + rh // 2
            d = day_label(ev["start"])
            if d != last_day or i == self.epg_top:
                c.create_text(pad, cy, anchor="w", text=d, font=self.f_small, fill=DIM)
                last_day = d
            live = ev["start"] <= t < ev["start"] + ev["dur"]
            c.create_text(pad + dw, cy, anchor="w", text=hhmm(ev["start"]),
                          font=self.f_bold, fill=ACCENT if live else TEXT)
            c.create_text(pad + dw + tw, cy, anchor="w", font=self.f_norm, fill=TEXT,
                          text=elide(self.f_norm, ev["title"], w - pad - dw - tw - 20))
        self._scrollbar(c, w, h, self.epg_top, rows, len(evs))

    def _draw_epg_info(self, c, w, h, pad):
        ch = self.epg_ch
        c.create_text(pad, pad, anchor="nw", text=elide(self.f_big, ch.name, w - 2 * pad),
                      font=self.f_big, fill=TEXT)
        y = pad + self.f_big.metrics("linespace") + 12
        if not self.epg_events:
            return
        ev = self.epg_events[max(0, min(self.epg_sel, len(self.epg_events) - 1))]
        c.create_text(pad, y, anchor="nw", text=day_label(ev["start"]), font=self.f_small, fill=DIM)
        y += self.f_small.metrics("linespace") + 4
        y = self._event_block(c, ev, pad, y, w - 2 * pad, show_bar=True)
        if ev["desc"]:
            c.create_text(pad, y + 4, anchor="nw", text=ev["desc"], font=self.f_small,
                          fill=DIM, width=w - 2 * pad)

    # ------------------------------------------------------------ dados
    def load_channels(self):
        self.status = "A carregar canais da box…"
        self.redraw()
        self.run_bg(self.owif.bouquets, self._channels_loaded, self.cfg.get("radio", False))

    def _channels_loaded(self, bouquets, err):
        if err or not bouquets:
            self.status = ("Não foi possível obter os canais da box:\n%s\n\n"
                           "Prime R para tentar de novo." % (err or "lista vazia"))
            log.error("canais: %s", err)
            self.redraw()
            return
        self.model.load(bouquets)
        log.info("%d bouquets, %d canais", len(self.model.bouquets), len(self.model.by_num))
        last = self.model.by_key.get(normref(self.cfg.get("last", "")))
        self.select_group(0)
        if last:
            self.play(last, last.group)
            self.go_full()
        else:
            self.focus = "groups"
            self.redraw()
        self.refresh_epg()

    def refresh_epg(self):
        for g in self.model.bouquets:
            if g.bref:
                self.run_bg(self.owif.epg_nownext,
                            lambda data, err, b=g.bref: self._epg_nownext(b, data, err), g.bref)
        self.root.after(10 * 60 * 1000, self.refresh_epg)

    def _epg_nownext(self, bref, data, err):
        if err:
            log.warning("EPG agora/seguinte falhou: %s", err)
            self.model.epg_time[bref] = time.time()
            return
        self.model.merge_epg(bref, data or {})
        self.redraw()

    def _refresh_stale_epg(self):
        chans = []
        if self.model.groups:
            chans = self.model.groups[self.gi].channels[self.ctop:self.ctop + 30]
        if self.playing:
            chans.append(self.playing)
        for b in self.model.stale_brefs(chans):
            self.model.epg_time[b] = time.time()
            self.run_bg(self.owif.epg_nownext,
                        lambda data, err, bb=b: self._epg_nownext(bb, data, err), b)

    # ------------------------------------------------------------ vídeo
    def _init_vlc(self):
        vlc = self.vlcmod
        self.vlc = vlc.Instance("--network-caching=1500", "--no-video-title-show", "--quiet")
        self.player = self.vlc.media_player_new()
        self.player.video_set_key_input(False)
        self.player.video_set_mouse_input(False)
        self.root.update_idletasks()
        wid = self.video.winfo_id()
        if sys.platform.startswith("win"):
            self.player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self.player.set_nsobject(wid)
        else:
            self.player.set_xwindow(wid)
        em = self.player.event_manager()
        em.event_attach(vlc.EventType.MediaPlayerEncounteredError,
                        lambda e: self.q.put((self._on_vlc_end, "erro", None)))
        em.event_attach(vlc.EventType.MediaPlayerEndReached,
                        lambda e: self.q.put((self._on_vlc_end, "fim", None)))

    def stream_url(self, ch):
        return "http://127.0.0.1:%d/stream/%s" % (
            self.proxy_port, urllib.parse.quote(ch.ref, safe=":"))

    def play(self, ch, group=None, keep_retry=False):
        if not ch:
            return
        if self.playing is not None and self.playing is not ch:
            self.prev = (self.playing, self.play_group)
        self.playing = ch
        self.play_group = group or self.play_group or ch.group
        if not keep_retry:
            self.retry = 0
        media = self.vlc.media_new(self.stream_url(ch))
        self.player.set_media(media)
        self.player.play()
        self.player.audio_set_volume(self.volume)
        self.root.after(800, lambda: self.player.audio_set_volume(self.volume))
        log.info("a reproduzir %d %s", ch.num, ch.name)
        self.save_cfg()
        self.redraw()
        self.show_infobar()

    def _on_vlc_end(self, what, _err):
        if not self.playing:
            return
        if self.retry < 3:
            self.retry += 1
            self.show_toast("Sinal perdido, a religar… (%d/3)" % self.retry)
            ch = self.playing
            self.root.after(1500 * self.retry,
                            lambda: self.playing is ch and self.play(ch, keep_retry=True))
        else:
            self.show_toast("Não foi possível reproduzir %s" % self.playing.name, 5)

    def zap(self, step):
        if not self.playing:
            return
        g = self.play_group or self.playing.group
        self.play(self.model.neighbour(g, self.playing, step), g)

    def set_volume(self, delta):
        self.volume = max(0, min(150, self.volume + delta))
        self.player.audio_set_volume(self.volume)
        self.show_toast("Volume %d%%" % self.volume)

    def toggle_mute(self):
        self.player.audio_toggle_mute()
        self.show_toast("Som desligado" if self.player.audio_get_mute() else "Som ligado")

    def _cycle_track(self, get_desc, get_cur, set_cur, label, allow_off):
        tracks = []
        for tid, name in (get_desc() or []):
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if tid == -1 and not allow_off:
                continue
            tracks.append((tid, name))
        if not tracks:
            self.show_toast("Sem %s" % label)
            return
        cur = get_cur()
        ids = [t[0] for t in tracks]
        i = (ids.index(cur) + 1) % len(ids) if cur in ids else 0
        set_cur(tracks[i][0])
        self.show_toast("%s: %s" % (label.capitalize(),
                                    "desligadas" if tracks[i][0] == -1 else tracks[i][1]))

    def cycle_audio(self):
        p = self.player
        self._cycle_track(p.audio_get_track_description, p.audio_get_track,
                          p.audio_set_track, "áudio", False)

    def cycle_subs(self):
        p = self.player
        self._cycle_track(p.video_get_spu_description, p.video_get_spu,
                          p.video_set_spu, "legendas", True)

    # ------------------------------------------------------------ toasts
    def show_toast(self, text, secs=2):
        self.toast.configure(text=text)
        self.toast.place(relx=0.97, rely=0.04, anchor="ne")
        self.toast.lift()
        if self.toast_job:
            self.root.after_cancel(self.toast_job)
        self.toast_job = self.root.after(secs * 1000, self.toast.place_forget)

    def number_key(self, d):
        if len(self.numbuf) >= 4:
            self.numbuf = ""
        self.numbuf += d
        ch = self.model.by_num.get(int(self.numbuf))
        self.numbox.configure(text=self.numbuf + ("  " + ch.name if ch else ""))
        self.numbox.place(relx=0.97, rely=0.04, anchor="ne")
        self.numbox.lift()
        if self.num_job:
            self.root.after_cancel(self.num_job)
        self.num_job = self.root.after(2000, self.number_commit)

    def number_commit(self):
        if self.num_job:
            self.root.after_cancel(self.num_job)
            self.num_job = None
        self.numbox.place_forget()
        buf, self.numbuf = self.numbuf, ""
        if not buf:
            return
        ch = self.model.by_num.get(int(buf))
        if not ch:
            self.show_toast("Canal %s não existe" % buf)
            return
        self.play(ch, ch.group)
        if self.mode == "list":
            self.open_list()
        else:
            self.show_infobar()

    # ------------------------------------------------------------ navegação
    def select_group(self, i):
        groups = self.model.groups
        if not groups:
            return
        self.gi = max(0, min(i, len(groups) - 1))
        chs = groups[self.gi].channels
        self.ci = chs.index(self.playing) if self.playing in chs else 0
        self.ctop = 0

    def move(self, step, page=False):
        if self.epg_ch:
            if self.epg_events:
                rows = max(1, self.ccanvas.winfo_height() //
                           int(self.f_norm.metrics("linespace") * 2.0))
                self.epg_sel = max(0, min(len(self.epg_events) - 1,
                                          self.epg_sel + step * (rows if page else 1)))
            self.redraw()
            return
        if not self.model.groups:
            return
        if self.focus == "groups":
            rows = max(1, self.gcanvas.winfo_height() //
                       int(self.f_norm.metrics("linespace") * 2.0))
            n = len(self.model.groups)
            i = self.gi + step * (rows if page else 1)
            self.select_group(i % n if not page else max(0, min(n - 1, i)))
        else:
            chs = self.model.groups[self.gi].channels
            rows = max(1, self.ccanvas.winfo_height() // self.row_h())
            n = len(chs)
            if n:
                i = self.ci + step * (rows if page else 1)
                self.ci = i % n if not page else max(0, min(n - 1, i))
        self.redraw()

    def on_click(self, kind, e, double):
        if self.mode != "list" or not self.model.groups:
            return
        if self.epg_ch and kind == "channels":
            if self.epg_events:
                rh = int(self.f_norm.metrics("linespace") * 2.0)
                self.epg_sel = min(len(self.epg_events) - 1, self.epg_top + e.y // rh)
                self.redraw()
            return
        self.epg_ch = None
        if kind == "groups":
            rh = int(self.f_norm.metrics("linespace") * 2.0)
            i = self.gtop + e.y // rh
            if i < len(self.model.groups):
                self.focus = "groups"
                self.select_group(i)
                if double:
                    self.focus = "channels"
        else:
            i = self.ctop + e.y // self.row_h()
            chs = self.model.groups[self.gi].channels
            if i < len(chs):
                self.focus = "channels"
                self.ci = i
                if double:
                    self.activate()
        self.redraw()

    def on_wheel(self, kind, step):
        if self.mode != "list":
            return
        if not self.epg_ch:
            self.focus = kind
        self.move(step)

    def activate(self):
        """OK na lista."""
        if self.focus == "groups":
            self.focus = "channels"
            self.redraw()
            return
        ch = self.cursor_channel()
        if not ch:
            return
        if ch is self.playing:
            self.go_full()
        else:
            self.play(ch, self.model.groups[self.gi])

    def toggle_fav(self, ch):
        if not ch:
            return
        cur_group = self.model.groups[self.gi] if self.model.groups else None
        fav = self.model.toggle_fav(ch)
        # mantém o grupo selecionado após reconstruir a lista de grupos
        if cur_group in self.model.groups:
            self.gi = self.model.groups.index(cur_group)
        else:
            self.select_group(min(self.gi, len(self.model.groups) - 1))
        self.save_cfg()
        self.show_toast(("★ %s adicionado aos favoritos" if fav
                         else "%s removido dos favoritos") % ch.name)
        self.redraw()

    def on_key(self, e):
        k = e.keysym
        kl = k.lower()
        if (e.state & 0x4) and kl == "q":
            return self.quit()
        if k == "F11":
            fs = not self.root.attributes("-fullscreen")
            self.root.attributes("-fullscreen", fs)
            return
        digit = k[3:] if k.startswith("KP_") and k[3:].isdigit() else k
        if digit.isdigit() and len(digit) == 1:
            return self.number_key(digit)
        if self.numbuf and k in ("Return", "KP_Enter"):
            return self.number_commit()
        if kl == "r" and not self.model.groups:
            return self.load_channels()
        # comuns
        if k in ("plus", "KP_Add", "equal"):
            return self.set_volume(5)
        if k in ("minus", "KP_Subtract"):
            return self.set_volume(-5)
        if kl == "m":
            return self.toggle_mute()
        if kl == "a":
            return self.cycle_audio()
        if kl == "s":
            return self.cycle_subs()
        if self.mode == "full":
            return self.key_full(k, kl)
        return self.key_list(k, kl)

    def key_full(self, k, kl):
        if k in ("Up", "Prior"):
            self.zap(1)
        elif k in ("Down", "Next"):
            self.zap(-1)
        elif k in ("Return", "KP_Enter"):
            self.open_list()
        elif k == "Left":
            self.set_volume(-5)
        elif k == "Right":
            self.set_volume(5)
        elif kl == "i":
            if self.infobar.winfo_ismapped():
                self.hide_infobar()
            else:
                self.show_infobar(10)
        elif kl in ("e", "g"):
            self.open_epg(self.playing)
        elif k == "BackSpace":
            if self.prev:
                ch, g = self.prev
                self.play(ch, g)
        elif kl == "f":
            self.toggle_fav(self.playing)
            self.show_infobar()
        elif k == "Escape":
            self.hide_infobar()

    def key_list(self, k, kl):
        if self.epg_ch:
            if k in ("Escape", "BackSpace", "Left") or kl in ("e", "g"):
                self.close_epg()
            elif k == "Up":
                self.move(-1)
            elif k == "Down":
                self.move(1)
            elif k == "Prior":
                self.move(-1, True)
            elif k == "Next":
                self.move(1, True)
            elif k in ("Return", "KP_Enter"):
                ch = self.epg_ch
                self.close_epg()
                if ch is not self.playing:
                    self.play(ch, self.model.groups[self.gi] if self.model.groups else None)
            return
        if k == "Up":
            self.move(-1)
        elif k == "Down":
            self.move(1)
        elif k == "Prior":
            self.move(-1, True)
        elif k == "Next":
            self.move(1, True)
        elif k == "Home":
            self.move(-10 ** 6, True)
        elif k == "End":
            self.move(10 ** 6, True)
        elif k == "Left":
            self.focus = "groups"
            self.redraw()
        elif k == "Right":
            self.focus = "channels"
            self.redraw()
        elif k in ("Return", "KP_Enter"):
            self.activate()
        elif k in ("Escape", "BackSpace"):
            if self.focus == "channels" and k == "BackSpace":
                self.focus = "groups"
                self.redraw()
            elif self.playing:
                self.go_full()
        elif kl in ("e", "g"):
            self.open_epg(self.cursor_channel())
        elif kl == "f":
            self.toggle_fav(self.cursor_channel())
        elif kl == "i" and self.playing:
            self.go_full()


# ----------------------------------------------------------------------------
# Arranque
# ----------------------------------------------------------------------------

def load_cfg():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser(description="Player de TV estilo MAG para Enigma2")
    ap.add_argument("--box", help="IP da box Enigma2")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--web-port", type=int)
    ap.add_argument("--stream-port", type=int)
    ap.add_argument("--radio", action="store_true", default=None,
                    help="incluir bouquets de rádio")
    ap.add_argument("--share", type=int, metavar="PORTA",
                    help="partilhar também o proxy na rede nesta porta (ex.: 8090)")
    ap.add_argument("--windowed", action="store_true", help="arrancar em janela")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if tk is None:
        sys.exit("Falta o Tkinter. No Linux: sudo apt install python3-tk")
    try:
        import vlc  # noqa: F401
    except (ImportError, OSError) as e:
        sys.exit("Falta o python-vlc ou o VLC (%s).\n"
                 "Instala o VLC e depois: pip install python-vlc" % e)

    cfg = load_cfg()
    for k in ("box", "user", "password", "web_port", "stream_port", "radio"):
        v = getattr(a, k)
        if v is not None:
            cfg[k] = v
    if not cfg.get("box"):
        sys.exit("Indica o IP da box na primeira vez: python3 e2player.py --box 192.168.1.65")
    cfg.setdefault("web_port", 80)
    cfg.setdefault("stream_port", 8001)

    pcfg = e2proxy.default_cfg(
        box=cfg["box"], web_port=cfg["web_port"], stream_port=cfg["stream_port"],
        user=cfg.get("user"), password=cfg.get("password"), radio=cfg.get("radio", False),
        listen="0.0.0.0" if a.share else "127.0.0.1", port=a.share or 0)
    _srv, port = e2proxy.start_server(pcfg)
    owif = OpenWebif(cfg["box"], cfg["web_port"], cfg.get("user"), cfg.get("password"))

    root = tk.Tk()
    root.title("e2player")
    if a.windowed:
        root.geometry("1280x720")
    else:
        root.attributes("-fullscreen", True)
    root.update_idletasks()
    App(root, cfg, owif, port)
    root.mainloop()


if __name__ == "__main__":
    main()
