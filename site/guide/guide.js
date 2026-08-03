/* Copy-to-clipboard for the code blocks in the install guides.
 *
 * Every button reads just "复制", so a screen-reader user hearing the list of
 * controls cannot tell which snippet each one copies — the caption beside it
 * is the only distinguishing text. Name each button from that caption, and
 * report the outcome through one shared live region instead of only mutating
 * the button label (which silently renames the control mid-interaction). */
const copyStatus = document.createElement("p");
copyStatus.className = "copy-status";
copyStatus.setAttribute("role", "status");
copyStatus.setAttribute("aria-live", "polite");
document.body.append(copyStatus);

function announce(message) {
  copyStatus.textContent = message;
  clearTimeout(announce.timer);
  announce.timer = setTimeout(() => { copyStatus.textContent = ""; }, 3000);
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  const caption = button.previousElementSibling?.textContent?.trim();
  if (caption && !button.hasAttribute("aria-label")) {
    button.setAttribute("aria-label", `复制：${caption}`);
  }

  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copy);
    if (!target) return;
    const label = caption || "代码";
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      const original = button.textContent;
      button.textContent = "已复制";
      announce(`已复制${label}`);
      setTimeout(() => { button.textContent = original; }, 1200);
    } catch {
      button.textContent = "请手动复制";
      announce(`复制失败，请手动选择${label}`);
    }
  });
});
