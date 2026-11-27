// Passes the chart readings to the bot running on this Mac. Nothing leaves your computer.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  fetch("http://127.0.0.1:8765/signal", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(msg),
  })
    .then((r) => r.json())
    .then((j) => reply({ok: true, text: j.text}))
    .catch(() => reply({ok: false, text: "Bot not running: start  python run.py watch"}));
  return true; // reply comes later
});
