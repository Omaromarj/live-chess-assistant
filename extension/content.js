// Chess Coach - content script (runs on chess.com).
// Reads the full move list and hands it to background.js whenever it changes.
(() => {
  // Plain SAN: e4, Nf3, exd5, O-O, e8=Q+, R1a3 ...
  const SAN = /^(O-O(-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=[QRBN])?)[+#]?$/;
  const BOARD = "wc-chess-board, chess-board";
  const MOVES_CONT = ".timestamps-with-base-time";

  function getMoves(movesCont) {
    const moves = [];

    const elements = movesCont.querySelectorAll(
      ".main-line-ply .node-highlight-content",
    );

    for (const element of elements) {
      const figurine = element.querySelector("[data-figurine]");

      let san = element.textContent.trim();

      if (figurine) {
        const piece = figurine.dataset.figurine;
        // Promotions show "e8=" followed by the piece icon (and maybe "+"),
        // every other move shows the piece icon first: icon + "f3".
        san = san.includes("=") ? san.replace("=", "=" + piece) : piece + san;
      }

      moves.push(san);
    }

    return moves;
  }

  let last = null; // JSON of the list we last sent successfully
  let blockedUntil = 0; // after a failed send, wait a moment before trying again
  let badReads = 0;
  let timer = 0;

  function readMoves() {
    // The move list container; if chess.com ever renames it, search the whole page instead.
    const movesCont = document.querySelector(MOVES_CONT) || document;
    const moves = getMoves(movesCont);
    // An empty list is only meaningful on a page that really has a board
    // (it means "new game, analyse the start position").
    if (moves.length === 0 && !document.querySelector(BOARD)) return null;
    return moves;
  }

  function check() {
    if (Date.now() < blockedUntil) return;

    const moves = readMoves();
    if (!moves) return;

    // The list can be caught half-rendered right after a move. Re-read a few
    // times before sending something that is not valid SAN; if it stays
    // invalid we send it anyway so the server window shows what is wrong.
    if (moves.some((m) => !SAN.test(m)) && badReads++ < 3) {
      setTimeout(check, 80);
      return;
    }
    badReads = 0;

    const key = JSON.stringify(moves);
    if (key === last) return;
    last = key;

    try {
      chrome.runtime.sendMessage({ type: "moves", moves }, (res) => {
        if (chrome.runtime.lastError || !res || !res.ok) {
          last = null; // not delivered: try again
          blockedUntil = Date.now() + 1000;
        }
      });
    } catch (e) {
      // Extension was reloaded or updated: refresh the chess.com tab.
    }
  }

  // The clocks make the page mutate constantly, so coalesce into one check.
  function schedule() {
    if (timer) return;
    timer = setTimeout(() => {
      timer = 0;
      check();
    }, 15);
  }

  new MutationObserver(schedule).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  setInterval(check, 500); // safety net if a mutation is ever missed
  setInterval(() => {
    last = null; // heartbeat: re-send the current list every 5 s so a restarted server catches up
  }, 5000);

  check();
})();
