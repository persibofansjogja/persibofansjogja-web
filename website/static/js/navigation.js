// Navigasi utama: menu mobile dan perilaku saat tautan dipilih.
(() => {
  const menuButton = document.querySelector(".menu-toggle");
  const nav = document.querySelector(".nav-links");

  if (!menuButton || !nav) return;

  const navLinks = nav.querySelectorAll(".nav-link");

  menuButton.addEventListener("click", () => {
    const isOpen = menuButton.getAttribute("aria-expanded") === "true";
    menuButton.setAttribute("aria-expanded", String(!isOpen));
    menuButton.setAttribute(
      "aria-label",
      isOpen ? "Buka menu navigasi" : "Tutup menu navigasi"
    );
    nav.classList.toggle("open", !isOpen);
  });

  navLinks.forEach((link) => {
    link.addEventListener("click", () => {
      nav.classList.remove("open");
      menuButton.setAttribute("aria-expanded", "false");
      menuButton.setAttribute("aria-label", "Buka menu navigasi");
    });
  });
})();
