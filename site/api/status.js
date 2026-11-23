"use strict";

module.exports = async function handler(req, res) {
  res.setHeader("Cache-Control", "s-maxage=30, stale-while-revalidate=60");

  try {
    const response = await fetch(
      "https://lichess.org/api/users/status?ids=duckish",
      { headers: { accept: "application/json" } }
    );

    if (!response.ok) {
      throw new Error("lichess status " + response.status);
    }

    const data = await response.json();
    const entry = Array.isArray(data) ? data[0] : null;

    if (!entry || typeof entry !== "object") {
      return res.status(200).json({ online: null, playing: null });
    }

    return res.status(200).json({
      online: entry.online === true,
      playing: entry.playing === true,
    });
  } catch (error) {
    return res.status(200).json({ online: null, playing: null });
  }
};