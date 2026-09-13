// Canvas-native instrument graphics: each workstation has its own visual language.
const themes = {
  clip: ['#101840', '#9cb9ff', 'EDIT / SEQUENCE 01'],
  ai: ['#102e30', '#98e2d5', 'VISION / FRAME SYNTHESIS'],
  longform: ['#302517', '#edcb87', 'DRAFT / STORYBOARD'],
  physics: ['#112b1c', '#9de5a7', 'LAB / MOTION STUDY'],
  enhance: ['#251d32', '#d0b7ee', 'MASTER / SIGNAL MONITOR'],
};
const clamp = n => Math.max(0, Math.min(1, n));

export function drawInstrument(ctx, mode, elapsed) {
  const [background, phosphor, title] = themes[mode.id] || themes.clip;
  const progress = clamp((elapsed - 750) / 1200);
  const reveal = 1 - Math.pow(1 - progress, 3);
  const travel = clamp((elapsed - 1300) / 2300);
  ctx.fillStyle = '#080d0c'; ctx.fillRect(0, 0, 1024, 768);
  if (!progress) return;
  ctx.save();
  ctx.beginPath(); ctx.rect(0, 384 * (1 - reveal), 1024, 768 * reveal); ctx.clip();
  ctx.fillStyle = background; ctx.fillRect(0, 0, 1024, 768);
  ctx.strokeStyle = phosphor; ctx.fillStyle = phosphor; ctx.lineWidth = 2;
  ctx.globalAlpha = .38; ctx.strokeRect(42, 40, 940, 687);
  ctx.globalAlpha = 1; ctx.font = '24px monospace'; ctx.fillText('SHORTSLAB', 76, 96);
  ctx.textAlign = 'right'; ctx.fillText('STUDIO SYSTEM', 948, 96); ctx.textAlign = 'left';
  ctx.fillRect(76, 126, 872, 2);
  ctx.font = 'bold 43px monospace'; ctx.fillText(mode.name.toUpperCase(), 76, 198);
  ctx.font = '20px monospace'; ctx.globalAlpha = .72; ctx.fillText(title, 76, 239); ctx.globalAlpha = 1;
  const x = 80, y = 292, w = 864, h = 266;
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w * clamp(travel * 2.4), h); ctx.clip();
  ctx.globalAlpha = .13;
  for (let i = 0; i <= 12; i++) {ctx.beginPath(); ctx.moveTo(x + i * 72, y); ctx.lineTo(x + i * 72, y + h); ctx.stroke();}
  for (let i = 0; i <= 4; i++) {ctx.beginPath(); ctx.moveTo(x, y + i * 66); ctx.lineTo(x + w, y + i * 66); ctx.stroke();}
  ctx.globalAlpha = 1;
  if (mode.id === 'clip') {
    for (let row = 0; row < 3; row++) {
      const cuts = row === 0 ? [0, 190, 410, 680, 864] : row === 1 ? [0, 270, 590, 864] : [0, 125, 465, 735, 864];
      cuts.slice(0, -1).forEach((start, i) => {
        ctx.globalAlpha = .2 + row * .17; ctx.fillRect(x + start + 3, y + 14 + row * 79, cuts[i + 1] - start - 6, 62);
        ctx.globalAlpha = .9; ctx.strokeRect(x + start + 3, y + 14 + row * 79, cuts[i + 1] - start - 6, 62);
      });
    }
    ctx.globalAlpha = 1; ctx.fillRect(x + 40 + travel * 650, y, 3, h);
  } else if (mode.id === 'ai') {
    for (let i = 0; i < 4; i++) {
      const left = x + i * 218;
      ctx.strokeRect(left + 3, y + 16, 199, 232);
      ctx.beginPath();
      for (let j = 0; j <= 40; j++) {
        const xx = left + 16 + j * 4.3, yy = y + 142 + Math.sin(j * .18 + i + travel * 2) * 42;
        if (!j) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
      }
      ctx.stroke(); ctx.font = '18px monospace'; ctx.fillText('FRAME 0' + (i + 1), left + 18, y + 48);
    }
  } else if (mode.id === 'longform') {
    for (let i = 0; i < 3; i++) {
      const left = x + i * 292, centre = left + 140;
      ctx.strokeRect(left + 3, y + 12, 268, 240);
      ctx.beginPath(); ctx.arc(centre, y + 76, 23, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(centre, y + 99); ctx.lineTo(centre, y + 166);
      ctx.moveTo(centre - 47, y + 138 - i * 14); ctx.lineTo(centre, y + 115); ctx.lineTo(centre + 47, y + 138 - i * 22);
      ctx.moveTo(centre - 37, y + 219); ctx.lineTo(centre, y + 166); ctx.lineTo(centre + 37, y + 219); ctx.stroke();
    }
  } else if (mode.id === 'physics') {
    ctx.lineWidth = 3;
    for (let wave = 0; wave < 2; wave++) {
      ctx.globalAlpha = wave ? .45 : 1; ctx.beginPath();
      for (let i = 0; i <= 240; i++) {
        const xx = x + i * w / 240, yy = y + h / 2 + Math.sin(i * .055 + travel * 2 + wave) * (90 - wave * 40);
        if (!i) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
      }
      ctx.stroke();
    }
  } else {
    for (let i = 0; i < 24; i++) {
      const value = .25 + .7 * Math.abs(Math.sin(i * .39 + travel * 1.4));
      for (let step = 0; step < 10; step++) {
        ctx.globalAlpha = step / 10 < value ? .85 : .12;
        ctx.fillRect(x + i * 36 + 3, y + h - 23 - step * 25, 25, 17);
      }
    }
  }
  ctx.restore(); ctx.globalAlpha = 1; ctx.fillStyle = phosphor;
  ctx.font = '22px monospace'; ctx.fillText(travel < 1 ? 'INITIALIZING STATION' : 'READY FOR YOUR NEXT PROJECT', 76, 623);
  ctx.globalAlpha = .65; ctx.font = '19px monospace'; ctx.fillText('CLICK SCREEN TO ENTER', 76, 683);
  ctx.fillStyle = '#000'; ctx.globalAlpha = .16;
  for (let i = 0; i < 768; i += 4) ctx.fillRect(0, i, 1024, 1);
  ctx.restore();
}
