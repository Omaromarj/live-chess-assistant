// Chess Coach - background service worker.
// Receives the move list from content.js and POSTs it to the local Python server.
// The server answers with an empty 204 and never sends data back.

const SERVER_URL = "http://127.0.0.1:8765/analyze"; // change the port here if you run the server with --port

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "moves") return;

  fetch(SERVER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: msg.moves }),
  })
    .then((r) => sendResponse({ ok: r.ok })) // 204: nothing to read, just report success
    .catch(() => sendResponse({ ok: false })); // server not running

  return true; // keep the message channel open for the async sendResponse
});
