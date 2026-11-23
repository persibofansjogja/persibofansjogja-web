// Interaksi khusus halaman beranda: tandai navigasi sesuai bagian yang terlihat.
(() => {
  const navLinks = document.querySelectorAll(".nav-link");
  const sections = document.querySelectorAll("main section[id]");

  if (!("IntersectionObserver" in window) || !sections.length || !navLinks.length) {
    return;
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;

      navLinks.forEach((link) => {
        link.classList.toggle(
          "active",
          link.getAttribute("href") === `#${entry.target.id}`
        );
      });
    });
  }, { rootMargin: "-35% 0px -55% 0px" });

  sections.forEach((section) => observer.observe(section));
})();
