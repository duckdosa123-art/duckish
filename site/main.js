(function () {
  "use strict";

  var reduceMotion = false;
  try {
    reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (error) {
    reduceMotion = false;
  }

  /* motion is filled in by loadAnimations(); null means "animate nothing". */
  var motion = null;

  /* ------------------------------------------------------------- status --- */

  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");
  var statusState = "unavailable";
  var dotPulse = null;

  function setStatus(state) {
    if (!statusDot || !statusText) {
      return;
    }
    statusState = state;
    statusDot.classList.remove("is-online", "is-offline");
    if (state === "online") {
      statusDot.classList.add("is-online");
      statusText.textContent = "online now";
    } else if (state === "offline") {
      statusDot.classList.add("is-offline");
      statusText.textContent = "offline";
    } else {
      statusText.textContent = "status unavailable";
    }
    syncStatusPulse();
  }

  function syncStatusPulse() {
    if (!statusDot || !motion) {
      return;
    }
    if (dotPulse && typeof dotPulse.stop === "function") {
      dotPulse.stop();
    }
    dotPulse = null;
    statusDot.style.transform = "";
    statusDot.style.opacity = "";
    if (statusState === "online") {
      dotPulse = motion.animate(
        statusDot,
        { scale: [1, 1.4, 1], opacity: [1, 0.55, 1] },
        { duration: 2.6, repeat: Infinity, ease: "easeInOut" }
      );
    }
  }

  function loadStatus() {
    if (!window.fetch) {
      setStatus("unavailable");
      return;
    }
    fetch("/api/status", { headers: { accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("status http " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        if (!data || typeof data.online !== "boolean") {
          setStatus("unavailable");
          return;
        }
        setStatus(data.online ? "online" : "offline");
      })
      .catch(function () {
        setStatus("unavailable");
      });
  }

  /* --------------------------------------------------------- challenge --- */

  var form = document.getElementById("challenge-form");
  var minutesInput = document.getElementById("minutes");
  var incrementInput = document.getElementById("increment");
  var colorSelect = document.getElementById("color");
  var variantSelect = document.getElementById("variant");
  var gameModeSelect = document.getElementById("game-mode");
  var previewEl = document.getElementById("preview-url");

  function buildChallengeUrl() {
    var params = new URLSearchParams();
    params.set("user", "duckish");
    params.set("variant", variantSelect ? variantSelect.value : "standard");
    params.set("gameMode", gameModeSelect ? gameModeSelect.value : "casual");

    var minutes = minutesInput ? parseFloat(minutesInput.value) : 10;
    if (!isFinite(minutes)) {
      minutes = 10;
    }
    if (minutes < 0.25) {
      minutes = 0.25;
    }
    if (minutes > 180) {
      minutes = 180;
    }

    var increment = incrementInput ? parseInt(incrementInput.value, 10) : 0;
    if (!isFinite(increment)) {
      increment = 0;
    }
    if (increment < 0) {
      increment = 0;
    }
    if (increment > 180) {
      increment = 180;
    }

    params.set("minutesPerSide", String(minutes));
    params.set("increment", String(increment));
    params.set("color", colorSelect ? colorSelect.value : "black");

    return "https://lichess.org/?" + params.toString() + "#friend";
  }

  function updatePreview() {
    if (!previewEl) {
      return;
    }
    var next = buildChallengeUrl();
    if (previewEl.textContent === next) {
      return;
    }
    previewEl.textContent = next;
    if (motion && !reduceMotion) {
      motion.animate(previewEl, { opacity: [0.45, 1] }, { duration: 0.24, ease: "easeOut" });
    }
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var url = buildChallengeUrl();
      if (previewEl) {
        previewEl.textContent = url;
      }
      window.open(url, "_blank", "noopener");
    });

    form.addEventListener("input", updatePreview);
    form.addEventListener("change", updatePreview);
    updatePreview();
  }

  /* -------------------------------------------------------- animation --- */

  var duckMark = document.getElementById("duck-mark");
  var duckSvg = duckMark ? duckMark.querySelector(".duck-svg") : null;
  var duckBob = duckMark ? duckMark.querySelector(".duck-bob") : null;
  var submitButton = document.getElementById("challenge-submit");
  var tweens = new WeakMap();

  /* Spring one element, cancelling whatever that element was doing before. */
  function springTo(element, target) {
    if (!motion || !element) {
      return;
    }
    var previous = tweens.get(element);
    if (previous && typeof previous.stop === "function") {
      previous.stop();
    }
    tweens.set(
      element,
      motion.animate(element, target, {
        type: "spring",
        stiffness: 300,
        damping: 20,
        mass: 0.6
      })
    );
  }

  function startDuckIdle(anime) {
    if (!anime || !duckBob) {
      return;
    }
    anime.animate(duckBob, {
      translateY: [-1.4, 1.4],
      rotate: [-1.8, 1.8],
      duration: 4600,
      ease: "inOutSine",
      loop: true,
      alternate: true
    });
  }

  function bindDuckHover() {
    if (!motion || !duckMark || !duckSvg) {
      return;
    }
    duckMark.addEventListener("pointerenter", function () {
      springTo(duckSvg, { rotate: -5, y: -2 });
    });
    duckMark.addEventListener("pointerleave", function () {
      springTo(duckSvg, { rotate: 0, y: 0 });
    });
  }

  function bindButtonPress() {
    if (!motion || !submitButton) {
      return;
    }
    submitButton.addEventListener("pointerdown", function () {
      springTo(submitButton, { scale: 0.97 });
    });
    var release = function () {
      springTo(submitButton, { scale: 1 });
    };
    submitButton.addEventListener("pointerup", release);
    submitButton.addEventListener("pointercancel", release);
    submitButton.addEventListener("pointerleave", release);
  }

  /* The page works without any of this: both imports are optional and a
     failed load just leaves motion/anime as null. */
  function loadAnimations() {
    if (reduceMotion) {
      return;
    }

    import("./vendor/anime.esm.js")
      .then(function (anime) {
        startDuckIdle(anime);
      })
      .catch(function () {
        /* no duck idle; everything else still runs */
      });

    import("./vendor/motion.esm.js")
      .then(function (mod) {
        motion = mod;
        bindDuckHover();
        bindButtonPress();
        syncStatusPulse();
      })
      .catch(function () {
        motion = null;
      });
  }

  loadStatus();
  loadAnimations();
})();
