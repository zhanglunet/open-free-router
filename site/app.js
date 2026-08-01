const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const revealItems = document.querySelectorAll(".reveal");

if (reduceMotion || !("IntersectionObserver" in window)) {
  revealItems.forEach((item) => item.classList.add("visible"));
} else {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("visible");
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1 });
  revealItems.forEach((item) => observer.observe(item));
}

const sections = [...document.querySelectorAll("main section[id]")];
const navLinks = [...document.querySelectorAll("nav a")];
if ("IntersectionObserver" in window) {
  const navObserver = new IntersectionObserver((entries) => {
    const current = entries.filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!current) return;
    navLinks.forEach((link) => link.toggleAttribute("aria-current", link.hash === `#${current.target.id}`));
  }, { rootMargin: "-35% 0px -55%", threshold: [0, 0.25, 0.75] });
  sections.forEach((section) => navObserver.observe(section));
}

fetch("/api/catalog", { headers: { Accept: "application/json" } })
  .then((response) => response.ok ? response.json() : Promise.reject(new Error(String(response.status))))
  .then((catalog) => {
    const values = {
      providers: catalog.provider_count,
      models: catalog.model_count,
      candidates: catalog.discovery?.candidate_model_count,
    };
    Object.entries(values).forEach(([key, value]) => {
      const target = document.querySelector(`[data-live="${key}"]`);
      if (target && Number.isFinite(value)) target.textContent = new Intl.NumberFormat("zh-CN").format(value);
    });
  })
  .catch(() => {});
