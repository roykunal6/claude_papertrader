// Reads the REAL TrendIkaterr indicator off the INDstocks chart (indstocks.com/app/flash).
// The indicator's legend value is coloured like its line: blue = bullish, red = bearish,
// green = recovery. Every 2 seconds we read that colour for each chart pane and send it to the bot.
(() => {
  const NAME = "TRENDIKATERR";

  function classify(rgb) {
    const m = (rgb || "").match(/\d+/g);
    if (!m) return null;
    const [r, g, b] = m.map(Number);
    if (b > r + 60 && b > g) return "BULLISH";      // blue  rgb(33,150,243)
    if (r > g + 60 && r > b + 60) return "BEARISH"; // red   rgb(229,57,53)
    if (g > r + 40 && g > b + 20) return "RECOVERY"; // green rgb(67,160,71)
    return null;
  }

  function readChart() {
    const panes = [];
    let hovering = false;
    for (const f of document.querySelectorAll('iframe[id^="tradingview_"]')) {
      let d;
      try { d = f.contentDocument; } catch (e) { continue; }
      if (!d) continue;
      // While the mouse is on the chart, the legend shows the candle under the mouse, not the latest one.
      if (f.matches(":hover")) { hovering = true; continue; }
      for (const t of d.querySelectorAll('[class*="title-"]')) {
        if (t.textContent.trim() !== NAME) continue;
        const item = t.closest('[class*="item-"]');
        const legend = t.closest('[class*="legend-"]');
        const v = item && item.querySelector('[class*="valueValue"]');
        if (!v || !legend) continue;
        const colour = getComputedStyle(v).color;
        panes.push({
          pane: legend.innerText.split("\n")[0].trim(),
          value: v.textContent.trim(),
          colour,
          state: classify(colour),
        });
      }
    }
    return {panes, hovering};
  }

  const badge = document.createElement("div");
  badge.style.cssText = "position:fixed;left:8px;bottom:8px;z-index:2147483647;padding:6px 10px;" +
    "border-radius:8px;font:12px/1.3 -apple-system,Segoe UI,sans-serif;color:#fff;background:#555;" +
    "box-shadow:0 2px 6px rgba(0,0,0,.3);max-width:520px;pointer-events:none";
  document.documentElement.appendChild(badge);
  const dot = {BULLISH: "🔵", BEARISH: "🔴", RECOVERY: "🟢"};

  function tick() {
    const r = readChart();
    const seen = r.panes.map((p) => `${p.pane}: ${dot[p.state] || "⚪"} ${p.state || p.colour}`).join(" · ");
    chrome.runtime.sendMessage({...r, visible: document.visibilityState, ts: Date.now()}, (res) => {
      if (chrome.runtime.lastError) return;
      const ok = res && res.ok;
      badge.style.background = ok ? "#1b5e20" : "#b71c1c";
      badge.textContent = "TrendIkaterr watcher · " +
        (r.hovering ? "paused while the mouse is on the chart · " : "") +
        (seen || "TRENDIKATERR not found on the chart") + " · " + (res ? res.text : "");
    });
  }
  setInterval(tick, 2000);
  tick();
})();
