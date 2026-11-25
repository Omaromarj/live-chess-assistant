#!/usr/bin/env python3
"""
Chess Coach - the best 3 moves for the side to move, live, in a small window.

    browser extension --POST {"moves": ["e4","Nc6","f4","f6","Nf3"]}--> this server
        --> Stockfish (MultiPV=3, kept running, hash kept warm) --> Tkinter window

The server never sends data back (every POST gets an empty 204). Results only
appear in the window. Send the full move list as often as you like: identical
lists are ignored in microseconds, a list that extends the last one only
re-analyses the new position, and any analysis still running is stopped the
moment a newer position arrives.

Setup
    pip install chess
    Stockfish binary: https://stockfishchess.org/download/
        -> put it next to this file, or on PATH, or pass --engine /path/to/stockfish
    Linux only:  sudo apt install python3-tk

Run
    python chess_coach.py
    (listens on http://127.0.0.1:8765 - use the IP, not "localhost", in the extension;
     on Windows "localhost" can add a noticeable delay per request)

Tuning (speed vs. strength)
    --time 2.0     max seconds spent on one position (results stream in long before that)
    --depth 25     max depth
    --threads N    default: CPU cores - 1
    --hash 256     Stockfish hash in MB
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import chess
    import chess.engine
except ImportError:
    sys.exit("Missing dependency. Run:  pip install chess")

try:
    import tkinter as tk
except ImportError:
    sys.exit("tkinter is missing. On Linux run:  sudo apt install python3-tk")


# --------------------------------------------------------------------------- #
#  Move parsing / game tracking
# --------------------------------------------------------------------------- #

_MOVE_NUMBER = re.compile(r"^\d+\.(\.\.)?")
_ANNOTATION = re.compile(r"[!?]+$")


def clean_move(m) -> str:
    """'1.e4' -> 'e4', 'Nf3!?' -> 'Nf3', '0-0' -> 'O-O'."""
    m = _MOVE_NUMBER.sub("", str(m).strip())
    m = _ANNOTATION.sub("", m).strip()
    return m.replace("0-0-0", "O-O-O").replace("0-0", "O-O")


def parse_move(board, text: str):
    """SAN first (what the extension sends); UCI ('e2e4') as a fallback."""
    try:
        return board.parse_san(text)
    except ValueError:
        pass
    try:
        mv = chess.Move.from_uci(text.lower())
        if mv in board.legal_moves:
            return mv
    except ValueError:
        pass
    raise ValueError(f"Bad move '{text}' at ply {len(board.move_stack) + 1}")


def over_text(board):
    o = board.outcome(claim_draw=False)
    if o is None:
        return None
    why = o.termination.name.replace("_", " ").title()
    if o.winner is None:
        return f"Draw - {why}"
    return f"{'White' if o.winner else 'Black'} wins - {why}"


class Game:
    """Remembers the last move list so a new one only costs the new moves."""

    def __init__(self):
        self.moves = None            # last accepted list (None = nothing yet)
        self.board = chess.Board()   # never mutated after being published
        self.game_id = 0             # bumps when the list is not a continuation
        self._bad = None             # last rejected list, so repeats stay quiet

    def update(self, raw):
        """-> None (nothing new) | ("error", msg) | ("ok", board, game_id)"""
        moves = [m for m in map(clean_move, raw) if m]
        if moves == self._bad or (self.moves is not None and moves == self.moves):
            return None
        prev = self.moves or []
        n = len(prev)
        same_game = moves[:n] == prev[:len(moves)]   # one is a prefix of the other
        try:
            if same_game and len(moves) > n:         # normal case: new moves only
                board = self.board.copy()
                for m in moves[n:]:
                    board.push(parse_move(board, m))
            else:                                    # new game / takeback: rebuild
                board = chess.Board()
                for m in moves:
                    board.push(parse_move(board, m))
        except ValueError as e:
            self._bad = moves
            return ("error", str(e))
        if not same_game:
            self.game_id += 1
        self.moves, self.board, self._bad = moves, board, None
        return ("ok", board, self.game_id)


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #

def find_stockfish(explicit=None):
    if explicit and os.path.isfile(explicit):
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("STOCKFISH"), shutil.which("stockfish")]
    cands += glob.glob(os.path.join(here, "stockfish*"))
    cands += glob.glob(os.path.join(here, "stockfish*", "stockfish*"))
    cands += ["/usr/games/stockfish", "/usr/local/bin/stockfish", "/opt/homebrew/bin/stockfish"]
    for c in cands:
        if c and os.path.isfile(c) and os.path.splitext(c)[1].lower() in ("", ".exe"):
            return c
    return r"C:\Users\InnJoo\Desktop\msharee3\python\projects\chessCheat\stockfish-windows-x86-64-universal\stockfish\stockfish-windows-x86-64-universal.exe"


def fmt_score(s) -> str:
    """s is a Score from the mover's point of view."""
    if s.is_mate():
        m = s.mate()
        return f"M{m}" if m > 0 else f"-M{abs(m)}"
    return f"{s.score() / 100:+.2f}"


class Analyzer(threading.Thread):
    """
    One Stockfish process, started once and kept hot. Only the newest position
    matters: submit() stops whatever is running and the worker picks up the
    latest job. Results stream out at every depth, so the first moves show up
    within milliseconds and get refined while you look at them.
    """

    def __init__(self, args, out):
        super().__init__(daemon=True, name="analyzer")
        self.args, self.out = args, out
        self.cv = threading.Condition()
        self.job = None          # (board, game_id, seq) - newest wins
        self.latest = 0          # seq of the newest request
        self.live = None         # analysis currently running
        self.engine = None
        self.closing = False
        self._failed_seq = None

    # ---- called from HTTP threads --------------------------------------- #
    def submit(self, board, game_id, seq):
        with self.cv:
            self.latest, self.job = seq, (board, game_id, seq)
            self._stop_live()
            self.cv.notify()

    def cancel(self, seq):
        with self.cv:
            self.latest, self.job = seq, None
            self._stop_live()

    def close(self):
        with self.cv:
            self.closing = True
            self.job = None
            self._stop_live()
            self.cv.notify()
        try:
            if self.engine is not None:
                self.engine.close()
        except Exception:
            pass

    def _stop_live(self):        # call with self.cv held (stop() does not block)
        if self.live is not None:
            try:
                self.live.stop()
            except Exception:
                pass

    # ---- worker thread --------------------------------------------------- #
    def _start_engine(self):
        if self.engine is not None:
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = None
        path = find_stockfish(self.args.engine)
        if not path:
            raise FileNotFoundError(
                "Stockfish not found.\nDownload it from stockfishchess.org, then put the "
                "file next to chess_coach.py, add it to PATH, or run with --engine PATH.")
        eng = chess.engine.SimpleEngine.popen_uci(path)
        for name, value in (("Threads", self.args.threads), ("Hash", self.args.hash)):
            try:
                eng.configure({name: value})
            except Exception:
                pass
        eng.analyse(chess.Board(), chess.engine.Limit(depth=8))   # warm-up: load net, alloc hash
        self.engine = eng
        label = eng.id.get("name", "Stockfish")
        self.out.put(("engine", f"{label} | {self.args.threads} threads | {self.args.hash} MB"))

    def run(self):
        try:
            self._start_engine()
        except Exception as e:
            self.out.put(("fatal", f"Could not start the engine.\n{e}"))
            return
        while True:
            with self.cv:
                while self.job is None and not self.closing:
                    self.cv.wait()
                if self.closing:
                    return
                board, game_id, seq = self.job
                self.job = None
            try:
                self._analyse(board, game_id, seq)
            except Exception as e:                      # engine died: restart, retry once
                if self.closing:
                    return
                self.out.put(("status", f"Engine error ({e}); restarting"))
                try:
                    self._start_engine()
                except Exception as e2:
                    self.out.put(("fatal", f"Engine will not restart.\n{e2}"))
                    return
                with self.cv:
                    if self.job is None and self.latest == seq and self._failed_seq != seq:
                        self.job = (board, game_id, seq)
                    self._failed_seq = seq

    def _analyse(self, board, game_id, seq):
        legal = board.legal_moves.count()
        n = min(3, legal)
        if legal == 1:
            limit = chess.engine.Limit(depth=6)          # forced move: no need to think long
        else:
            limit = chess.engine.Limit(time=self.args.time, depth=self.args.depth)
        t0 = time.perf_counter()
        lines = {}
        # game=game_id keeps Stockfish's hash table between moves of the same game
        with self.engine.analysis(board, limit, multipv=n, game=game_id) as ana:
            with self.cv:
                self.live = ana
                if self.latest != seq:                   # a newer move arrived meanwhile
                    self._stop_live()
            try:
                for info in ana:
                    if self.latest != seq:
                        continue                         # stale: drain until engine is idle
                    pv, score = info.get("pv"), info.get("score")
                    if not pv or score is None or info.get("lowerbound") or info.get("upperbound"):
                        continue
                    idx = info.get("multipv", 1)
                    lines[idx] = info
                    if idx == n and len(lines) == n:     # one full depth iteration done
                        self.out.put(("result", self._payload(board, seq, lines, t0)))
            finally:
                with self.cv:
                    self.live = None

    @staticmethod
    def _payload(board, seq, lines, t0):
        out = []
        for idx in sorted(lines):
            info = lines[idx]
            pv = info["pv"]
            s = info["score"].pov(board.turn)
            try:
                line = board.variation_san(pv[:7])
            except ValueError:
                line = board.san(pv[0])
            out.append({
                "san": board.san(pv[0]),
                "uci": pv[0].uci(),
                "score": fmt_score(s),
                "cp": s.score(mate_score=10000),
                "pv": line,
                "pv_uci": [m.uci() for m in pv[:7]],
            })
        return {
            "seq": seq,
            "turn": board.turn,
            "move_no": board.fullmove_number,
            "depth": min(i.get("depth", 0) for i in lines.values()),
            "ms": int((time.perf_counter() - t0) * 1000),
            "lines": out,
            "fen": board.fen(),
            "last_uci": board.peek().uci() if board.move_stack else None,
        }


# --------------------------------------------------------------------------- #
#  HTTP server (extension -> here). Always answers 204, never with data.
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, out, analyzer):
        self.out, self.analyzer = out, analyzer
        self.game = Game()
        self.lock = threading.Lock()
        self.seq = 0

    def on_moves(self, raw):
        with self.lock:
            r = self.game.update(raw)
            if r is None:
                return
            if r[0] == "error":
                self.out.put(("error", r[1]))
                return
            _, board, game_id = r
            self.seq += 1
            over = over_text(board)
            self.out.put(("position", {
                "seq": self.seq,
                "turn": board.turn,
                "move_no": board.fullmove_number,
                "over": over,
                "fen": board.fen(),
                "last_uci": board.peek().uci() if board.move_stack else None,
            }))
            if over:
                self.analyzer.cancel(self.seq)
            else:
                self.analyzer.submit(board, game_id, self.seq)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive: repeated POSTs reuse one connection
    server_version = "ChessCoach"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *a):
        pass

    def _reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):                  # CORS preflight from a content script
        self._reply(204)

    def do_GET(self):                      # handy to check in a browser that it is up
        self._reply(200, b"Chess Coach is running. POST {\"moves\": [...]} here.\n")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            moves = data["moves"]
            if not isinstance(moves, list):
                raise ValueError
        except Exception:
            self._reply(400)
            return
        self._reply(204)                   # answer first: the extension never waits on us
        self.server.app.on_moves(moves)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows this flag would allow double-binding


# --------------------------------------------------------------------------- #
#  Tk window
# --------------------------------------------------------------------------- #

BG, CARD, BORDER = "#14161b", "#1e2128", "#2c303a"
TEXT, MUTED = "#eceff4", "#8b93a3"
GREEN, RED = "#5fd38d", "#ef6b6b"
ACCENTS = ("#5fd38d", "#e8c468", "#7ea6ff")
FONT = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
MONO = "Consolas" if sys.platform.startswith("win") else ("Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono")
PIECE_FONT = "Segoe UI Symbol" if sys.platform.startswith("win") else MONO

PIECE_GLYPH = {"P": "♟", "N": "♞", "B": "♝", "R": "♜", "Q": "♛", "K": "♚"}


class BoardView(tk.Canvas):
    """A static chess board: squares, coordinates, pieces, and move arrows."""

    SQ = 44
    MARGIN = 16
    LIGHT, DARK = "#30343d", "#20232b"
    LAST_L, LAST_D = "#55502a", "#4a461f"
    CHECK = "#5a2020"
    W_FILL, W_OUT = "#f2f4f8", "#0b0c0f"
    B_FILL, B_OUT = "#14161b", "#c7cdd9"

    def __init__(self, parent):
        side = self.MARGIN * 2 + self.SQ * 8
        super().__init__(parent, width=side, height=side, bg=BG, highlightthickness=0)

    def clear(self):
        self.delete("all")

    def _xy(self, square, flipped):
        file, rank = chess.square_file(square), chess.square_rank(square)
        col = (7 - file) if flipped else file
        row = rank if flipped else (7 - rank)
        x = self.MARGIN + col * self.SQ
        y = self.MARGIN + row * self.SQ
        return x, y

    def _center(self, square, flipped):
        x, y = self._xy(square, flipped)
        return x + self.SQ / 2, y + self.SQ / 2

    def _seg_arrow(self, x1, y1, x2, y2, color, dashed):
        dx, dy = x2 - x1, y2 - y1
        dist = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / dist, dy / dist
        sx, sy = x1 + ux * 10, y1 + uy * 10
        ex, ey = x2 - ux * 14, y2 - uy * 14
        self.create_line(sx, sy, ex, ey, fill=color, width=5, capstyle=tk.ROUND,
                          arrow=tk.LAST, arrowshape=(12, 14, 6),
                          dash=(4, 3) if dashed else None)

    def _arrow(self, frm, to, color, dashed, flipped):
        ff, fr = chess.square_file(frm), chess.square_rank(frm)
        tf, tr = chess.square_file(to), chess.square_rank(to)
        if {abs(tf - ff), abs(tr - fr)} == {1, 2}:          # knight move: draw the elbow
            elbow = chess.square(tf, fr) if abs(tf - ff) == 2 else chess.square(ff, tr)
            fx, fy = self._center(frm, flipped)
            ex, ey = self._center(elbow, flipped)
            self.create_line(fx, fy, ex, ey, fill=color, width=5, capstyle=tk.ROUND,
                              dash=(4, 3) if dashed else None)
            tx, ty = self._center(to, flipped)
            self._seg_arrow(ex, ey, tx, ty, color, dashed)
        else:
            fx, fy = self._center(frm, flipped)
            tx, ty = self._center(to, flipped)
            self._seg_arrow(fx, fy, tx, ty, color, dashed)

    def _outlined_text(self, x, y, char, fill, outline, font):
        for ox, oy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)):
            self.create_text(x + ox, y + oy, text=char, font=font, fill=outline)
        self.create_text(x, y, text=char, font=font, fill=fill)

    def draw(self, fen, last_uci=None, arrows=(), flipped=False):
        """arrows: iterable of (from_square, to_square, color, dashed)."""
        self.delete("all")
        board = chess.Board(fen)
        last_from = last_to = None
        if last_uci:
            mv = chess.Move.from_uci(last_uci)
            last_from, last_to = mv.from_square, mv.to_square
        check_sq = board.king(board.turn) if board.is_check() else None

        for sq in chess.SQUARES:
            x, y = self._xy(sq, flipped)
            light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
            color = self.LIGHT if light else self.DARK
            if sq in (last_from, last_to):
                color = self.LAST_L if light else self.LAST_D
            if sq == check_sq:
                color = self.CHECK
            self.create_rectangle(x, y, x + self.SQ, y + self.SQ, fill=color, width=0)

        for col in range(8):
            file_idx = (7 - col) if flipped else col
            x = self.MARGIN + col * self.SQ + self.SQ / 2
            y = self.MARGIN + 8 * self.SQ + 9
            self.create_text(x, y, text="abcdefgh"[file_idx], fill=MUTED, font=(FONT, 8))
        for row in range(8):
            rank_idx = row if flipped else (7 - row)
            x = self.MARGIN - 9
            y = self.MARGIN + row * self.SQ + self.SQ / 2
            self.create_text(x, y, text=str(rank_idx + 1), fill=MUTED, font=(FONT, 8))

        font = (PIECE_FONT, int(self.SQ * 0.62), "bold")
        for sq, piece in board.piece_map().items():
            cx, cy = self._center(sq, flipped)
            glyph = PIECE_GLYPH[piece.symbol().upper()]
            fill, outline = (self.W_FILL, self.W_OUT) if piece.color else (self.B_FILL, self.B_OUT)
            self._outlined_text(cx, cy, glyph, fill, outline, font)

        for frm, to, color, dashed in arrows:
            self._arrow(frm, to, color, dashed, flipped)


class Card(tk.Frame):
    def __init__(self, parent, rank, accent, on_click=None):
        super().__init__(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, cursor="hand2")
        self.accent = accent
        self.rank = tk.Label(self, text=str(rank), font=(FONT, 22, "bold"), fg=accent, bg=CARD, width=2)
        self.rank.grid(row=0, column=0, rowspan=2, padx=(10, 4), pady=(8, 0))
        self.san = tk.Label(self, text="-", font=(FONT, 20, "bold"), fg=MUTED, bg=CARD, anchor="w")
        self.san.grid(row=0, column=1, sticky="w")
        self.sq = tk.Label(self, text="", font=(MONO, 10), fg=MUTED, bg=CARD, anchor="w")
        self.sq.grid(row=1, column=1, sticky="w")
        self.score = tk.Label(self, text="", font=(MONO, 15, "bold"), fg=TEXT, bg=CARD)
        self.score.grid(row=0, column=2, rowspan=2, padx=12, sticky="e")
        self.pv = tk.Label(self, text="", font=(MONO, 9), fg=MUTED, bg=CARD,
                           anchor="w", justify="left", wraplength=330)
        self.pv.grid(row=2, column=0, columnspan=3, sticky="we", padx=12, pady=(0, 8))
        self.columnconfigure(1, weight=1)
        if on_click:
            for w in (self, self.rank, self.san, self.sq, self.score, self.pv):
                w.bind("<Button-1>", lambda e: on_click())

    def clear(self):
        self.san.config(text="-", fg=MUTED)
        for w in (self.sq, self.score, self.pv):
            w.config(text="")
        self.rank.config(fg=MUTED)

    def dim(self):
        for w in (self.rank, self.san, self.score):
            w.config(fg=MUTED)

    def show(self, line, dim=False):
        u = line["uci"]
        cp = line["cp"]
        self.san.config(text=line["san"], fg=MUTED if dim else TEXT)
        self.sq.config(text=f"{u[:2]} -> {u[2:4]}" + (f"  = {u[4:].upper()}" if len(u) > 4 else ""))
        self.score.config(text=line["score"],
                          fg=MUTED if dim else (GREEN if cp >= 30 else RED if cp <= -30 else TEXT))
        self.pv.config(text=line["pv"])
        self.rank.config(fg=MUTED if dim else self.accent)

    def set_selected(self, selected):
        self.configure(highlightbackground=self.accent if selected else BORDER,
                       highlightthickness=2 if selected else 1)


class UI:
    def __init__(self, root, args, q, analyzer, server):
        self.root, self.q, self.analyzer, self.server = root, q, analyzer, server
        self.cur_seq = 0
        self.cur_pos = None           # latest "position" payload (always set once a game starts)
        self.last = None              # latest "result" payload for cur_seq
        self.engine_text = ""
        self.focus_line = None        # index of the card being stepped through, or None (= live)
        self.step_idx = 0             # how many of that line's moves are currently applied
        self.side = tk.StringVar(value="both")
        self.on_top = tk.BooleanVar(value=True)

        root.title("Chess Coach")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Left>", lambda e: self.step(-1))
        root.bind("<Right>", lambda e: self.step(1))
        root.bind("<Escape>", lambda e: self.go_live())

        main = tk.Frame(root, bg=BG)
        main.pack(fill="both", expand=True)

        left = tk.Frame(main, bg=BG)
        left.pack(side="left", padx=(14, 8), pady=14)

        self.board = BoardView(left)
        self.board.pack()
        self.board.draw(chess.STARTING_FEN)

        self.step_lbl = tk.Label(left, text="", font=(FONT, 9), fg=MUTED, bg=BG, wraplength=380, justify="left")
        self.step_lbl.pack(fill="x", pady=(6, 0))

        stepctl = tk.Frame(left, bg=BG)
        stepctl.pack(pady=(4, 0))
        self.prev_btn = tk.Button(stepctl, text="< Prev", command=lambda: self.step(-1), state="disabled",
                                  font=(FONT, 9), bg=CARD, fg=TEXT, activebackground=BORDER,
                                  activeforeground=TEXT, relief="flat", padx=8)
        self.prev_btn.pack(side="left", padx=2)
        self.live_btn = tk.Button(stepctl, text="Live", command=self.go_live, state="disabled",
                                  font=(FONT, 9), bg=CARD, fg=TEXT, activebackground=BORDER,
                                  activeforeground=TEXT, relief="flat", padx=8)
        self.live_btn.pack(side="left", padx=2)
        self.next_btn = tk.Button(stepctl, text="Next >", command=lambda: self.step(1), state="disabled",
                                  font=(FONT, 9), bg=CARD, fg=TEXT, activebackground=BORDER,
                                  activeforeground=TEXT, relief="flat", padx=8)
        self.next_btn.pack(side="left", padx=2)

        tk.Label(left, text="Click a line to replay it · ←/→ to step · Esc for live",
                font=(FONT, 8), fg=MUTED, bg=BG).pack(pady=(6, 0))

        right = tk.Frame(main, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(0, 14), pady=14)

        self.header = tk.Label(right, text="Waiting for moves...", font=(FONT, 16, "bold"),
                               fg=TEXT, bg=BG, anchor="w", justify="left", wraplength=350)
        self.header.pack(fill="x")
        self.sub = tk.Label(right, text=f"Listening on http://{args.host}:{args.port}",
                            font=(FONT, 9), fg=MUTED, bg=BG, anchor="w", justify="left", wraplength=350)
        self.sub.pack(fill="x", pady=(0, 8))

        self.cards = [Card(right, i + 1, ACCENTS[i], on_click=lambda i=i: self.toggle_focus(i)) for i in range(3)]
        for c in self.cards:
            c.pack(fill="x", pady=4)

        ctl = tk.Frame(right, bg=BG)
        ctl.pack(fill="x", pady=(6, 0))
        tk.Label(ctl, text="I play:", font=(FONT, 9), fg=MUTED, bg=BG).pack(side="left")
        for text, val in (("Both", "both"), ("White", "white"), ("Black", "black")):
            tk.Radiobutton(ctl, text=text, value=val, variable=self.side, command=self.render,
                           font=(FONT, 9), fg=TEXT, bg=BG, selectcolor=CARD, activebackground=BG,
                           activeforeground=TEXT, highlightthickness=0, bd=0).pack(side="left", padx=2)
        tk.Checkbutton(ctl, text="On top", variable=self.on_top,
                       command=lambda: root.attributes("-topmost", self.on_top.get()),
                       font=(FONT, 9), fg=TEXT, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=TEXT, highlightthickness=0, bd=0).pack(side="right")

        self.footer = tk.Label(right, text="Starting engine...", font=(FONT, 8), fg=MUTED, bg=BG,
                               anchor="w", justify="left", wraplength=350)
        self.footer.pack(fill="x", pady=(6, 10))

        self.poll()

    # ---- queue from server/engine threads -> UI ----
    def poll(self):
        newest = None
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "result":
                    newest = data              # only the latest result matters
                else:
                    self.handle(kind, data)
        except queue.Empty:
            pass
        if newest is not None and newest["seq"] == self.cur_seq:
            self.last = newest
            self.render()
        self.root.after(15, self.poll)

    def handle(self, kind, data):
        if kind == "position":
            self.cur_seq = data["seq"]
            self.cur_pos = data
            self.last = None
            self.focus_line = None        # the position moved on; stop replaying a stale line
            self.step_idx = 0
            self.footer.config(text=self.engine_text, fg=MUTED)
            self.render()
        elif kind == "engine":
            self.engine_text = data
            self.footer.config(text=data, fg=MUTED)
        elif kind == "status":
            self.footer.config(text=data, fg=MUTED)
        elif kind == "error":
            self.footer.config(text=data, fg=RED)
        elif kind == "fatal":
            self.header.config(text="Engine problem", fg=RED)
            self.sub.config(text=data)
            for c in self.cards:
                c.clear()
                c.set_selected(False)
            self.board.clear()
            self._set_step_controls(enabled=False)

    # ---- line stepping ----
    def toggle_focus(self, i):
        if self.last is None or i >= len(self.last["lines"]):
            return
        if self.focus_line == i:
            self.go_live()
        else:
            self.focus_line, self.step_idx = i, 0
            self.render()

    def go_live(self):
        if self.focus_line is not None:
            self.focus_line, self.step_idx = None, 0
            self.render()

    def step(self, delta):
        if self.focus_line is None or self.last is None:
            return
        pv = self.last["lines"][self.focus_line].get("pv_uci", [])
        self.step_idx = max(0, min(len(pv), self.step_idx + delta))
        self.render()

    def _set_step_controls(self, enabled, can_prev=False, can_next=False):
        self.live_btn.config(state="normal" if enabled else "disabled")
        self.prev_btn.config(state="normal" if enabled and can_prev else "disabled")
        self.next_btn.config(state="normal" if enabled and can_next else "disabled")

    # ---- rendering ----
    def render(self):
        pos = self.cur_pos
        if pos is None:
            return
        flipped = self.side.get() == "black"

        if pos.get("over"):
            self.header.config(text=pos["over"], fg=TEXT)
            self.sub.config(text="Game over")
            for c in self.cards:
                c.clear()
                c.set_selected(False)
            self.board.draw(pos["fen"], pos.get("last_uci"), [], flipped)
            self.step_lbl.config(text="")
            self._set_step_controls(enabled=False)
            return

        turn = pos["turn"]
        side = self.side.get()
        mine = side == "both" or (side == "white") == turn
        who = "White" if turn else "Black"
        self.header.config(text=f"{who} to move" if mine else f"Opponent's turn ({who})",
                           fg=TEXT if mine else MUTED)

        r = self.last if (self.last and self.last["seq"] == self.cur_seq) else None

        if r is None:
            self.sub.config(text=f"Move {pos['move_no']} - thinking...")
            for c in self.cards:
                c.dim()
                c.set_selected(False)
            self.board.draw(pos["fen"], pos.get("last_uci"), [], flipped)
            self.step_lbl.config(text="")
            self._set_step_controls(enabled=False)
            return

        self.sub.config(text=f"Move {r['move_no']} - depth {r['depth']} - {r['ms']} ms"
                             + ("" if mine else " - their likely moves"))

        if self.focus_line is not None and self.focus_line < len(r["lines"]):
            line = r["lines"][self.focus_line]
            pv = line.get("pv_uci", [])
            self.step_idx = max(0, min(self.step_idx, len(pv)))
            board = chess.Board(r["fen"])
            applied_last = r.get("last_uci")
            for k in range(self.step_idx):
                mv = chess.Move.from_uci(pv[k])
                applied_last = mv.uci()
                board.push(mv)
            arrows = []
            if self.step_idx < len(pv):
                mv = chess.Move.from_uci(pv[self.step_idx])
                arrows = [(mv.from_square, mv.to_square, ACCENTS[self.focus_line], False)]
            self.board.draw(board.fen(), applied_last, arrows, flipped)
            self.step_lbl.config(text=f"Line {self.focus_line + 1} — {line['san']}: "
                                      f"ply {self.step_idx}/{len(pv)}")
            self._set_step_controls(enabled=True, can_prev=self.step_idx > 0, can_next=self.step_idx < len(pv))
        else:
            arrows = []
            for i, ln in enumerate(r["lines"]):
                mv = chess.Move.from_uci(ln["uci"])
                arrows.append((mv.from_square, mv.to_square, ACCENTS[i], not mine))
            self.board.draw(r["fen"], r.get("last_uci"), arrows, flipped)
            self.step_lbl.config(text="")
            self._set_step_controls(enabled=False)

        for i, card in enumerate(self.cards):
            if i < len(r["lines"]):
                card.show(r["lines"][i], dim=not mine)
            else:
                card.clear()
            card.set_selected(i == self.focus_line)

    def close(self):
        self.analyzer.close()
        try:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        except Exception:
            pass
        self.root.destroy()


# --------------------------------------------------------------------------- #

def parse_args():
    p = argparse.ArgumentParser(description="Chess Coach: best 3 moves via Stockfish")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--engine", help="path to the Stockfish binary")
    p.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--hash", type=int, default=256, help="Stockfish hash, MB")
    p.add_argument("--time", type=float, default=2.0, help="max seconds per position")
    p.add_argument("--depth", type=int, default=25, help="max depth per position")
    return p.parse_args()


def main():
    args = parse_args()
    out = queue.Queue()
    analyzer = Analyzer(args, out)
    try:
        server = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot listen on {args.host}:{args.port} ({e}). Is another copy already running?")
    server.app = App(out, analyzer)
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    analyzer.start()
    print(f"Chess Coach listening on http://{args.host}:{args.port}")

    root = tk.Tk()
    UI(root, args, out, analyzer, server)
    root.mainloop()


if __name__ == "__main__":
    main()