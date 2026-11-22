#!/usr/bin/env python3
"""
tvhplayer - player de TV estilo MAG (a mesma interface do e2player) para
servidores TVHeadend. Sem proxy: o VLC liga-se diretamente ao TVHeadend.

  * grupos = etiquetas de canais (channel tags) do TVHeadend
    (+ Favoritos e Todos os canais)
  * números dos canais = os do TVHeadend
  * EPG agora/seguinte e guia completo vindos do TVHeadend
  * ícones dos canais (imagecache do TVHeadend)
  * com utilizador/palavra-passe, o stream usa um "ticket" (não expõe a palavra-passe)

Requisitos: os mesmos do e2player (Python 3.8+ com Tkinter, VLC, python-vlc,
opcional pillow) e os ficheiros e2player.py e e2proxy.py na mesma pasta.

Uso:
  python tvhplayer.py --server 192.168.1.10 [--port 9981] [--user u --password p]
                      [--profile pass]
  (fica guardado em ~/.tvhplayer.json; nas vezes seguintes basta  python tvhplayer.py)

As teclas são as mesmas do e2player (Ctrl+Q para sair).
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2player  # noqa: E402
from e2proxy import normref, clean_name  # noqa: E402

log = logging.getLogger("tvhplayer")

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".tvhplayer.json")
ALL = "ALL"   # o EPG do TVHeadend pede-se de uma vez para todos os canais


class TvhSource:
    """Fonte de canais TVHeadend (API HTTP/JSON)."""
    title = "tvhplayer"
    sort_all_by_num = True

    def __init__(self, host, port=9981, user=None, password=None, profile="pass", timeout=20):
        self.host, self.port = host, port
        self.base = "http://%s:%d" % (host, port)
        self.user, self.password = user or "", password or ""
        self.profile = profile or "pass"
        self.timeout = timeout
        self.icons = {}
        handlers = []
        if self.user:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.base, self.user, self.password)
            handlers += [urllib.request.HTTPDigestAuthHandler(mgr),
                         urllib.request.HTTPBasicAuthHandler(mgr)]
        self.opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------ HTTP
    def get(self, path, params=None, timeout=None):
        data = urllib.parse.urlencode(params).encode() if params else None   # POST
        req = urllib.request.Request(self.base + path, data=data)
        with self.opener.open(req, timeout=timeout or self.timeout) as r:
            return r.read()

    def api(self, path, params=None, timeout=None):
        raw = self.get(path, params, timeout).decode("utf-8", "replace")
        return json.loads(raw, strict=False)

    # ------------------------------------------------------------ canais
    def bouquets(self, radio=False):
        tags = self.api("/api/channeltag/grid", {"limit": 10000}).get("entries", [])
        chans = self.api("/api/channel/grid", {"limit": 100000}, timeout=60).get("entries", [])
        chans = [c for c in chans if c.get("enabled", True)]

        def num_of(c):
            try:
                return int(float(c.get("number") or 0))
            except (TypeError, ValueError):
                return 0
        # canais sem número ficam no fim, numerados a seguir ao maior
        nxt = max([num_of(c) for c in chans] + [0])
        nums = {}
        for c in sorted(chans, key=lambda c: (num_of(c) == 0, num_of(c), (c.get("name") or "").lower())):
            n = num_of(c)
            if n <= 0:
                nxt += 1
                n = nxt
            nums[c["uuid"]] = n
            icon = c.get("icon_public_url") or c.get("icon") or ""
            if icon:
                self.icons[normref(c["uuid"])] = icon
        ordered = sorted(chans, key=lambda c: (nums[c["uuid"]], (c.get("name") or "").lower()))

        groups = []
        tags = [t for t in tags if t.get("enabled", True) and not t.get("internal", False)]
        tags.sort(key=lambda t: (t.get("index", 0), (t.get("name") or "").lower()))
        for t in tags:
            members = [(c["uuid"], clean_name(c.get("name")), nums[c["uuid"]])
                       for c in ordered if t["uuid"] in (c.get("tags") or [])]
            if members:
                groups.append((clean_name(t.get("name")), ALL, members))
        if not groups:
            groups.append(("Canais", ALL,
                           [(c["uuid"], clean_name(c.get("name")), nums[c["uuid"]]) for c in ordered]))
        log.info("TVHeadend: %d canais, %d etiquetas", len(ordered), len(groups))
        return groups

    # ------------------------------------------------------------ EPG
    @staticmethod
    def _event(e):
        parts = []
        for k in ("subtitle", "summary", "description"):
            v = clean_name(e.get(k))
            if v and v not in parts:
                parts.append(v)
        start, stop = int(e.get("start") or 0), int(e.get("stop") or 0)
        return {"ref": e.get("channelUuid", ""), "title": clean_name(e.get("title")),
                "start": start, "dur": max(0, stop - start), "desc": "\n".join(parts),
                "next": e.get("nextEventId")}

    def epg_nownext(self, bref):
        """Agora (mode=now) + seguinte (epg/events/load) de todos os canais."""
        now = [self._event(e) for e in
               self.api("/api/epg/events/grid", {"mode": "now", "limit": 100000}, 60).get("entries", [])]
        ids = [e["next"] for e in now if e.get("next")]
        nxt = []
        for i in range(0, len(ids), 200):
            try:
                chunk = self.api("/api/epg/events/load", {"eventId": json.dumps(ids[i:i + 200])}, 60)
                nxt += [self._event(e) for e in chunk.get("entries", [])]
            except Exception as e:   # o "seguinte" é opcional
                log.warning("EPG seguinte falhou: %s", e)
                break
        out = {}
        for ev in now + nxt:
            if ev["start"] > 0:
                out.setdefault(normref(ev["ref"]), []).append(ev)
        for v in out.values():
            v.sort(key=lambda ev: ev["start"])
        return out

    def epg_service(self, ref):
        evs = [self._event(e) for e in
               self.api("/api/epg/events/grid", {"channel": ref, "limit": 500}, 60).get("entries", [])]
        return sorted([e for e in evs if e["start"] > 0], key=lambda e: e["start"])

    # ------------------------------------------------------------ ícones
    def picon(self, ref):
        url = self.icons.get(normref(ref))
        if not url:
            return None
        try:
            if url.startswith("http://") or url.startswith("https://"):
                if url.startswith(self.base):
                    data = self.get(url[len(self.base):], timeout=10)
                else:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        data = r.read()
            else:
                data = self.get("/" + url.lstrip("/"), timeout=10)
        except Exception:
            return None
        if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"GIF8":
            return data
        return None

    # ------------------------------------------------------------ stream
    def _with_profile(self, url):
        sep = "&" if "?" in url else "?"
        return url + sep + "profile=" + urllib.parse.quote(self.profile)

    def stream_url(self, ref):
        path = "/stream/channel/" + ref
        if not self.user:
            return self._with_profile(self.base + path)
        # com autenticação: pede uma playlist com ticket (válido 5 min para iniciar)
        try:
            m3u = self.get("/playlist/ticket/channel/" + ref, timeout=10).decode("utf-8", "replace")
            for line in m3u.splitlines():
                line = line.strip()
                if line.startswith("http") and "ticket=" in line:
                    u = urllib.parse.urlsplit(line)
                    # usa sempre o nosso endereço (o TVHeadend pode devolver outro nome)
                    return self._with_profile(self.base + u.path + "?" + u.query)
            log.warning("playlist sem ticket - a usar credenciais no URL")
        except Exception as e:
            log.warning("ticket falhou (%s) - a usar credenciais no URL", e)
        cred = "%s:%s@" % (urllib.parse.quote(self.user, safe=""), urllib.parse.quote(self.password, safe=""))
        return self._with_profile("http://%s%s:%d%s" % (cred, self.host, self.port, path))


def main():
    ap = argparse.ArgumentParser(description="Player de TV estilo MAG para TVHeadend")
    ap.add_argument("--server", help="IP ou nome do servidor TVHeadend")
    ap.add_argument("--port", type=int, help="porta HTTP do TVHeadend (9981)")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--profile", help="perfil de streaming do TVHeadend (pass)")
    ap.add_argument("--windowed", action="store_true", help="arrancar em janela")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    tk = e2player.tk
    if tk is None:
        sys.exit("Falta o Tkinter. No Linux: sudo apt install python3-tk")
    try:
        import vlc  # noqa: F401
    except (ImportError, OSError) as e:
        sys.exit("Falta o python-vlc ou o VLC (%s).\nInstala o VLC e depois: pip install python-vlc" % e)

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    for k in ("server", "port", "user", "password", "profile"):
        v = getattr(a, k)
        if v is not None:
            cfg[k] = v
    if not cfg.get("server"):
        sys.exit("Indica o servidor na primeira vez: python tvhplayer.py --server 192.168.1.10")
    cfg.setdefault("port", 9981)
    cfg.setdefault("profile", "pass")

    src = TvhSource(cfg["server"], cfg["port"], cfg.get("user"), cfg.get("password"), cfg["profile"])
    root = tk.Tk()
    root.title("tvhplayer")
    if a.windowed:
        root.geometry("1280x720")
    else:
        root.attributes("-fullscreen", True)
    root.update_idletasks()
    e2player.App(root, cfg, src, config_path=CONFIG_PATH)
    root.mainloop()


if __name__ == "__main__":
    main()
