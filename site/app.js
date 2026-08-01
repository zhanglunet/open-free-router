const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

document.querySelectorAll(".copy").forEach((button) => {
  button.addEventListener("click", async () => {
    const code = button.parentElement.querySelector("code")?.textContent ?? "";
    try {
      await navigator.clipboard.writeText(code);
      button.textContent = "COPIED";
      button.classList.add("copied");
      window.setTimeout(() => {
        button.textContent = "COPY";
        button.classList.remove("copied");
      }, 1600);
    } catch {
      button.textContent = "SELECT";
    }
  });
});

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
  }, { threshold: 0.12 });
  revealItems.forEach((item) => observer.observe(item));
}

const sections = [...document.querySelectorAll("main section[id]")];
const navLinks = [...document.querySelectorAll("nav a")];
if ("IntersectionObserver" in window) {
  const navObserver = new IntersectionObserver((entries) => {
    const current = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!current) return;
    navLinks.forEach((link) => link.toggleAttribute("aria-current", link.hash === `#${current.target.id}`));
  }, { rootMargin: "-35% 0px -55%", threshold: [0, 0.25, 0.75] });
  sections.forEach((section) => navObserver.observe(section));
}
