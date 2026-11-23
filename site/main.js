(function () {
  "use strict";

  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");

  function setStatus(state) {
    if (!statusDot || !statusText) {
      return;
    }
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
    params.set("color", colorSelect ? colorSelect.value : "white");

    return "https://lichess.org/?" + params.toString() + "#friend";
  }

  function updatePreview() {
    if (previewEl) {
      previewEl.textContent = buildChallengeUrl();
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

  loadStatus();
})();