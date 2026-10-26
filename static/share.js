// Draws a shareable workout card (PNG) on a canvas and hands it to the share sheet.
// window.TimGymShare.share(data) where data is the JSON the workout day page embeds.
(() => {
  const W = 1080, H = 1350, PAD = 72;
  const FONT = '-apple-system, "SF Pro Display", "Segoe UI", system-ui, sans-serif';
  const C = {
    bg: "#090d0b", card: "#111815", line: "#26322b", text: "#f2f6f1", muted: "#9ba99f", accent: "#c7f36a",
    heart: "#ff6b6b", groups: {Legs: "#c7f36a", Chest: "#ff8a65", Back: "#75c8ff", Shoulders: "#c69cff", Arms: "#ffb35c", Core: "#5fd3b0"},
  };

  const font = (weight, size) => `${weight} ${size}px ${FONT}`;

  function fit(ctx, text, maxWidth) {
    if (ctx.measureText(text).width <= maxWidth) return text;
    let cut = text;
    while (cut.length > 1 && ctx.measureText(cut + "…").width > maxWidth) cut = cut.slice(0, -1);
    return cut.trimEnd() + "…";
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function loadImage(src) {
    return new Promise(resolve => {
      if (!src) return resolve(null);
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => resolve(null);
      image.src = src;
    });
  }

  function drawCover(ctx, image, x, y, size, radius) {
    ctx.save();
    roundRect(ctx, x, y, size, size, radius);
    ctx.clip();
    if (image) {
      const scale = Math.max(size / image.width, size / image.height);
      const w = image.width * scale, h = image.height * scale;
      ctx.drawImage(image, x + (size - w) / 2, y + (size - h) / 2, w, h);
    } else {
      ctx.fillStyle = "#1c2620";
      ctx.fillRect(x, y, size, size);
    }
    ctx.restore();
  }

  function formatDuration(seconds) {
    if (!seconds) return null;
    const minutes = Math.round(seconds / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}` : `${minutes}`;
  }

  async function render(data) {
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");
    const images = await Promise.all(data.exercises.map(exercise => loadImage(exercise.image)));

    // Background with a soft accent glow.
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, W, H);
    const glow = ctx.createRadialGradient(140, 80, 0, 140, 80, 760);
    glow.addColorStop(0, "rgba(199,243,106,0.16)");
    glow.addColorStop(1, "rgba(199,243,106,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, H);

    let y = PAD + 8;
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = C.accent;
    ctx.font = font(800, 30);
    ctx.fillText("TIMGYM", PAD, y + 22);
    ctx.fillStyle = C.muted;
    ctx.font = font(600, 30);
    ctx.textAlign = "right";
    ctx.fillText(data.date, W - PAD, y + 22);
    ctx.textAlign = "left";

    y += 120;
    ctx.fillStyle = C.text;
    ctx.font = font(800, 104);
    ctx.fillText(fit(ctx, data.label, W - 2 * PAD), PAD, y);
    if (data.subtitle) {
      y += 54;
      ctx.fillStyle = C.muted;
      ctx.font = font(500, 34);
      ctx.fillText(fit(ctx, data.subtitle, W - 2 * PAD), PAD, y);
    }

    // Stat boxes: duration, calories, average and max heart rate.
    const stats = [
      ["Duration", formatDuration(data.duration_s), data.duration_s >= 3600 ? "" : "min"],
      ["Calories", data.kcal ? Math.round(data.kcal).toLocaleString() : null, "kcal"],
      ["Avg HR", data.avg_hr, "bpm"],
      ["Max HR", data.max_hr, "bpm"],
    ].filter(stat => stat[1] !== null && stat[1] !== undefined);
    if (!stats.length) stats.push(["Sets", data.sets, "sets"], ["Volume", Math.round(data.volume).toLocaleString(), "kg"]);
    y += 44;
    const gap = 20, boxW = (W - 2 * PAD - gap * (stats.length - 1)) / stats.length, boxH = 150;
    stats.forEach(([label, value, unit], index) => {
      const x = PAD + index * (boxW + gap);
      ctx.fillStyle = C.card;
      roundRect(ctx, x, y, boxW, boxH, 28);
      ctx.fill();
      ctx.fillStyle = C.muted;
      ctx.font = font(700, 24);
      ctx.fillText(label.toUpperCase(), x + 26, y + 44);
      ctx.fillStyle = label.endsWith("HR") ? C.heart : C.text;
      ctx.font = font(800, 56);
      const text = String(value);
      ctx.fillText(text, x + 26, y + 112);
      const valueWidth = ctx.measureText(text).width;
      ctx.fillStyle = C.muted;
      ctx.font = font(600, 24);
      ctx.fillText(unit, x + 32 + valueWidth, y + 112);
    });
    y += boxH + 36;

    // Heart rate over the session.
    if (data.hr && data.hr.length > 1) {
      const chartH = 150, low = Math.min(...data.hr) - 5, high = Math.max(...data.hr) + 5;
      const points = data.hr.map((bpm, index) => [PAD + (W - 2 * PAD) * index / (data.hr.length - 1), y + chartH - (bpm - low) / (high - low) * chartH]);
      const fill = ctx.createLinearGradient(0, y, 0, y + chartH);
      fill.addColorStop(0, "rgba(255,107,107,0.35)");
      fill.addColorStop(1, "rgba(255,107,107,0)");
      ctx.beginPath();
      points.forEach(([px, py], index) => index ? ctx.lineTo(px, py) : ctx.moveTo(px, py));
      ctx.lineTo(W - PAD, y + chartH);
      ctx.lineTo(PAD, y + chartH);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.beginPath();
      points.forEach(([px, py], index) => index ? ctx.lineTo(px, py) : ctx.moveTo(px, py));
      ctx.strokeStyle = C.heart;
      ctx.lineWidth = 5;
      ctx.lineJoin = "round";
      ctx.stroke();
      y += chartH + 36;
    }

    // Muscle groups trained.
    if (data.groups && data.groups.length) {
      let x = PAD;
      const barW = W - 2 * PAD;
      ctx.save();
      roundRect(ctx, PAD, y, barW, 18, 9);
      ctx.clip();
      for (const [label, percent] of data.groups) {
        ctx.fillStyle = C.groups[label] || C.muted;
        ctx.fillRect(x, y, barW * percent / 100, 18);
        x += barW * percent / 100;
      }
      ctx.restore();
      y += 58;
      x = PAD;
      ctx.font = font(600, 28);
      for (const [label, percent] of data.groups) {
        const text = `${label} ${percent}%`;
        ctx.fillStyle = C.groups[label] || C.muted;
        ctx.beginPath();
        ctx.arc(x + 9, y - 9, 9, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = C.muted;
        ctx.fillText(text, x + 26, y);
        x += 26 + ctx.measureText(text).width + 34;
      }
      y += 40;
    }

    // Exercises with their photos: one column, or two when that's what it takes to fit them.
    const footerTop = H - PAD - 40, rowH = 104;
    const rows = Math.max(1, Math.floor((footerTop - y - 10) / rowH));
    const columns = data.exercises.length > rows ? 2 : 1;
    const colW = (W - 2 * PAD - (columns - 1) * 32) / columns;
    const room = rows * columns;
    const shown = data.exercises.length > room ? room - 1 : data.exercises.length;
    const perColumn = Math.ceil(shown / columns);
    data.exercises.slice(0, shown).forEach((exercise, index) => {
      const left = PAD + Math.floor(index / perColumn) * (colW + 32), top = y + (index % perColumn) * rowH;
      drawCover(ctx, images[index], left, top, 84, 20);
      ctx.fillStyle = C.text;
      ctx.font = font(700, columns > 1 ? 32 : 36);
      ctx.fillText(fit(ctx, exercise.name, colW - 108), left + 108, top + 38);
      ctx.fillStyle = C.muted;
      ctx.font = font(500, columns > 1 ? 28 : 30);
      ctx.fillText(fit(ctx, exercise.summary, colW - 108), left + 108, top + 78);
    });
    if (shown < data.exercises.length) {
      ctx.fillStyle = C.muted;
      ctx.font = font(600, 30);
      ctx.fillText(`+${data.exercises.length - shown} more`, PAD + colW + 32, y + (rows - 1) * rowH + 54);
    }

    // Footer totals.
    ctx.fillStyle = C.line;
    ctx.fillRect(PAD, footerTop - 26, W - 2 * PAD, 2);
    ctx.fillStyle = C.text;
    ctx.font = font(700, 32);
    if (data.exercises.length) ctx.fillText(`${data.exercises.length} exercises · ${data.sets} sets${data.volume ? ` · ${Math.round(data.volume).toLocaleString()} kg` : ""}`, PAD, footerTop + 30);
    ctx.fillStyle = C.accent;
    ctx.font = font(800, 30);
    ctx.textAlign = "right";
    ctx.fillText("TimGym", W - PAD, footerTop + 30);
    ctx.textAlign = "left";

    return new Promise(resolve => canvas.toBlob(resolve, "image/png"));
  }

  // Call with an already rendered blob from a tap: iOS only opens the share sheet
  // while the tap is fresh, so the slow drawing happens before (see the day page).
  async function share(data, blob) {
    blob = blob || await render(data);
    const name = `timgym-${data.slug || "workout"}.png`;
    const file = new File([blob], name, {type: "image/png"});
    if (navigator.canShare && navigator.canShare({files: [file]})) {
      try {
        await navigator.share({files: [file], title: data.label});
        return "shared";
      } catch (error) {
        if (error.name === "AbortError") return "cancelled";
      }
    }
    // No share sheet (most desktops): download the image instead.
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    return "downloaded";
  }

  window.TimGymShare = {render, share};
})();
