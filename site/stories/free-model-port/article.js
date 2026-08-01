const copyButton = document.querySelector("#copy-moment");
const copyStatus = document.querySelector("#copy-status");

copyButton?.addEventListener("click", async () => {
  const text = document.querySelector("#moment-copy")?.textContent?.trim() || "";
  try {
    await navigator.clipboard.writeText(text);
    copyStatus.textContent = "已复制，可以打开朋友圈粘贴了";
    copyButton.textContent = "朋友圈文案已复制 ✓";
  } catch (_error) {
    copyStatus.textContent = "浏览器未允许自动复制，请长按上方文案复制";
  }
});
