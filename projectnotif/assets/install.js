
(() => {
  let installPrompt = null;

  const buttons = [
    ...document.querySelectorAll("[data-install-app]")
  ];

  const guidance = [
    ...document.querySelectorAll("[data-install-guidance]")
  ];

  const isIos =
    /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (
      navigator.platform === "MacIntel" &&
      navigator.maxTouchPoints > 1
    );

  const isAndroid = /android/i.test(navigator.userAgent);

  const isMobile = isIos || isAndroid;

  function isStandalone() {
    return (
      window.matchMedia("(display-mode: standalone)").matches ||
      navigator.standalone === true
    );
  }

  function showGuidance(message) {
    guidance.forEach(element => {
      element.textContent = message;
      element.hidden = false;
    });
  }

  function hideGuidance() {
    guidance.forEach(element => {
      element.hidden = true;
    });
  }

  function updateInstallButton() {
    const shouldShow =
      !isStandalone() && (isMobile || Boolean(installPrompt));

    buttons.forEach(button => {
      button.hidden = !shouldShow;
    });

    if (isStandalone()) {
      hideGuidance();
    }
  }

  // Daftarkan Service Worker hanya pada konteks yang aman.
  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker
        .register("/service-worker.js")
        .catch(error => {
          console.error(
            "Pendaftaran Service Worker gagal:",
            error
          );
        });
    });
  }

  // Android/Chrome dapat menyediakan dialog instalasi bawaan.
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    installPrompt = event;
    updateInstallButton();
  });

  // Saat aplikasi berhasil dipasang.
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    updateInstallButton();

    showGuidance(
      "Aplikasi berhasil dipasang di perangkat ini."
    );
  });

  buttons.forEach(button => {
    button.addEventListener("click", async () => {
      hideGuidance();

      if (isStandalone()) {
        updateInstallButton();
        return;
      }

      // Gunakan dialog instalasi bawaan jika tersedia.
      if (installPrompt) {
        try {
          const promptEvent = installPrompt;
          installPrompt = null;

          await promptEvent.prompt();
          const choice = await promptEvent.userChoice;

          if (choice.outcome === "accepted") {
            updateInstallButton();
          } else {
            // Jika dibatalkan, tombol tetap tersedia.
            updateInstallButton();
            showGuidance(
              "Instalasi belum dilakukan. Kamu bisa mencoba lagi melalui tombol Pasang Aplikasi."
            );
          }
        } catch (error) {
          console.error("Instalasi gagal:", error);
          updateInstallButton();
          showGuidance(
            "Dialog instalasi tidak dapat dibuka. Coba gunakan menu browser untuk memasang aplikasi."
          );
        }

        return;
      }

      // Petunjuk instalasi iPhone/iPad.
      if (isIos) {
        if (!window.isSecureContext) {
          showGuidance(
            "Pemasangan PWA memerlukan HTTPS yang valid. Setelah HTTPS aktif, buka alamat ini di Safari, ketuk Bagikan, lalu pilih Tambahkan ke Layar Utama."
          );
        } else {
          showGuidance(
            "Buka halaman ini di Safari, ketuk Bagikan, lalu pilih Tambahkan ke Layar Utama."
          );
        }

        return;
      }

      // Petunjuk instalasi Android.
      if (isAndroid) {
        if (!window.isSecureContext) {
          showGuidance(
            "Pemasangan PWA memerlukan HTTPS yang valid. Setelah HTTPS aktif, buka alamat ini di Chrome."
          );
        } else {
          showGuidance(
            "Buka menu Chrome (⋮), lalu pilih Pasang aplikasi atau Tambahkan ke layar utama jika tersedia."
          );
        }

        return;
      }

      showGuidance(
        "Jika browser mendukung instalasi aplikasi, gunakan menu browser lalu pilih opsi Pasang aplikasi."
      );
    });
  });

  // Perbarui tampilan jika mode aplikasi berubah.
  const displayMode = window.matchMedia(
    "(display-mode: standalone)"
  );

  if (displayMode.addEventListener) {
    displayMode.addEventListener("change", updateInstallButton);
  } else if (displayMode.addListener) {
    displayMode.addListener(updateInstallButton);
  }

  updateInstallButton();
})();
