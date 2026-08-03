const header = document.querySelector(".topbar");

if (header) {
  const globalLinks = [
    ["首页", "/"],
    ["免费模型", "/models/"],
    ["模型评测", "/benchmarks/"],
    ["工具比较", "/compare/"],
    ["准入机制", "/validation/"],
    ["实时状态", "/status/"],
    ["系统架构", "/architecture/"],
    ["全球分布", "/map/"],
    ["安装指南", "/guide/"],
    ["npm 安装", "/guide/npm/"],
    ["推荐文章", "/stories/free-model-port/"],
    ["开发日志", "/logs/"],
    ["品牌页面", "/brand/"],
    ["站点地图", "/sitemap/"],
  ];
  const currentPath = window.location.pathname.replace(/\/index\.html$/, "/");
  const pageLinks = [...header.querySelectorAll(":scope > nav a")]
    .map((link) => [link.textContent.trim(), link.getAttribute("href") || ""])
    .filter(([, href]) => href.startsWith("#"));

  const toggle = document.createElement("button");
  toggle.className = "mobile-nav-toggle";
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "mobile-navigation");
  toggle.setAttribute("aria-label", "打开网站导航");
  toggle.innerHTML = "<span></span><span></span><span></span>";

  const panel = document.createElement("div");
  panel.id = "mobile-navigation";
  panel.className = "mobile-nav-panel";
  panel.hidden = true;
  // The panel covers the page when open, so announce it as a modal dialog
  // rather than an anonymous div appended to the end of <body>.
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", "网站导航");
  const globalNav = document.createElement("nav");
  globalNav.setAttribute("aria-label", "移动端主导航");
  globalNav.innerHTML = globalLinks.map(([label, href], index) =>
    `<a href="${href}"><small>${String(index + 1).padStart(2, "0")}</small><span>${label}</span></a>`
  ).join("");
  // Mark exactly one link. A plain prefix test matches several entries at
  // once (/guide/npm/ satisfies both "/guide/" and "/guide/npm/", and "/"
  // prefixes everything), which announces multiple "current page" links to
  // assistive tech. The longest matching prefix is the real one, and an
  // exact match is by definition the longest, so one pass covers both.
  const navLinks = [...globalNav.querySelectorAll("a")];
  const bestPath = navLinks
    .map((link) => new URL(link.href, window.location.origin).pathname)
    .filter((linkPath) => (linkPath === "/" ? currentPath === "/" : currentPath.startsWith(linkPath)))
    .reduce((best, linkPath) => (linkPath.length > best.length ? linkPath : best), "");
  if (bestPath) {
    const match = navLinks.find(
      (link) => new URL(link.href, window.location.origin).pathname === bestPath,
    );
    match?.setAttribute("aria-current", "page");
  }
  panel.append(globalNav);

  if (pageLinks.length) {
    const section = document.createElement("div");
    section.className = "mobile-page-links";
    const title = document.createElement("p");
    title.textContent = "本页目录";
    section.append(title);
    pageLinks.forEach(([label, href]) => {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = label;
      section.append(link);
    });
    panel.append(section);
  }

  function setOpen(open) {
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "关闭网站导航" : "打开网站导航");
    toggle.classList.toggle("open", open);
    panel.hidden = !open;
    document.body.classList.toggle("mobile-nav-open", open);
    // The panel is the last child of <body>; without moving focus into it a
    // keyboard user would tab through the whole page before reaching the
    // menu they just opened, and land behind the overlay on close.
    if (open) panel.querySelector("a")?.focus();
    else toggle.focus();
  }

  toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
  panel.addEventListener("click", (event) => { if (event.target.closest("a")) setOpen(false); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) setOpen(false);
    if (event.key !== "Tab" || panel.hidden) return;
    // The dialog's members are not contiguous in the DOM: the toggle lives in
    // the header while the panel is the last child of <body>, so the page's
    // whole tab order sits between them. Guarding only the two ends of the
    // list therefore leaks — Tab from the toggle walked into <main> and
    // Shift+Tab from the first link walked into the footer. Take over Tab
    // entirely while the dialog is open and drive the cycle ourselves.
    const items = [toggle, ...panel.querySelectorAll("a")];
    const index = items.indexOf(document.activeElement);
    event.preventDefault();
    const step = event.shiftKey ? -1 : 1;
    const next = index === -1
      ? items[event.shiftKey ? items.length - 1 : 0] // focus escaped: pull it back
      : items[(index + step + items.length) % items.length];
    next.focus();
  });
  window.matchMedia("(min-width: 981px)").addEventListener("change", (event) => { if (event.matches) setOpen(false); });
  header.append(toggle);
  document.body.append(panel);
}
