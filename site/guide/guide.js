document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copy);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      const old = button.textContent;
      button.textContent = "已复制";
      setTimeout(() => { button.textContent = old; }, 1200);
    } catch {
      button.textContent = "请手动复制";
    }
  });
});
