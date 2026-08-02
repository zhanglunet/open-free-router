const header = document.querySelector(".topbar");

if (header) {
  const globalLinks = [
    ["首页", "/"],
    ["免费模型", "/models/"],
    ["实时状态", "/status/"],
    ["系统架构", "/architecture/"],
    ["全球分布", "/map/"],
    ["安装指南", "/guide/"],
    ["推荐文章", "/stories/free-model-port/"],
    ["开发日志", "/logs/"],
    ["品牌页面", "/brand/"],
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
  const globalNav = document.createElement("nav");
  globalNav.setAttribute("aria-label", "移动端主导航");
  globalNav.innerHTML = globalLinks.map(([label, href], index) =>
    `<a href="${href}"><small>${String(index + 1).padStart(2, "0")}</small><span>${label}</span></a>`
  ).join("");
  [...globalNav.querySelectorAll("a")].forEach((link) => {
    const linkPath = new URL(link.href, window.location.origin).pathname;
    const active = linkPath === "/" ? currentPath === "/" : currentPath.startsWith(linkPath);
    if (active) link.setAttribute("aria-current", "page");
  });
  if (!globalNav.querySelector('[aria-current="page"]')) {
    globalNav.querySelector("a")?.setAttribute("aria-current", "page");
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
  }

  toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
  panel.addEventListener("click", (event) => { if (event.target.closest("a")) setOpen(false); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") setOpen(false); });
  window.matchMedia("(min-width: 981px)").addEventListener("change", (event) => { if (event.matches) setOpen(false); });
  header.append(toggle);
  document.body.append(panel);
}
