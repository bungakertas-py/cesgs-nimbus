/* Skew-T log-P: termodinamika parcel + render diagram klasik.
 * Dipakai kartu "Profil Atmosfer" di panel titik. Data profil (T/RH/angin per
 * level tekanan) diberikan frontend; modul ini menghitung Td, jalur parcel,
 * LCL/LFC/EL, CAPE/CIN, lalu menggambar SVG (isoterm miring, adiabat kering/
 * basah, garis rasio campuran, T/Td/parcel, arsir CAPE/CIN, wind barbs).
 *
 * CATATAN: indikasi model GFS (grid ~1 derajat), bukan sounding radiosonde asli.
 */
(function () {
  "use strict";

  // ---- Konstanta termodinamika ----
  const Rd = 287.05, cp = 1005.0, kappa = Rd / cp;   // 0.2854
  const Lv = 2.5e6, EPS = 0.622;
  const K0 = 273.15, MS_TO_KT = 1.943844;

  // Tekanan uap jenuh (hPa) atas air, T dalam °C (Bolton 1980).
  function esat(Tc) { return 6.112 * Math.exp(17.67 * Tc / (Tc + 243.5)); }

  // Titik embun (°C) dari T(°C) & RH(%).
  function dewpoint(Tc, RH) {
    const e = Math.max(esat(Tc) * Math.max(RH, 0.01) / 100, 1e-6);
    const ln = Math.log(e / 6.112);
    return 243.5 * ln / (17.67 - ln);
  }

  // Rasio campuran (kg/kg) dari tekanan uap e(hPa) & tekanan p(hPa).
  function mixRatio(e, p) { return EPS * e / Math.max(p - e, 1e-6); }

  // Suhu virtual (K) dari T(°C) & rasio campuran r(kg/kg).
  function Tvirt(Tc, r) { return (Tc + K0) * (1 + 0.608 * r); }

  // LCL: dari parcel permukaan (p0 hPa, T0 °C, Td0 °C) -> {p, T(°C)} (Bolton eq.15+Poisson).
  function lcl(p0, T0, Td0) {
    const TK = T0 + K0, TdK = Td0 + K0;
    const Tl = 1 / (1 / (TdK - 56) + Math.log(TK / TdK) / 800) + 56;   // K
    const pl = p0 * Math.pow(Tl / TK, 1 / kappa);
    return { p: pl, T: Tl - K0 };
  }

  // Laju adiabat basah pseudo dT/d(ln p) (K), T dalam K, p hPa.
  function moistDToDlnp(TK, p) {
    const Tc = TK - K0;
    const es = esat(Tc);
    const rs = mixRatio(es, p);
    return (Rd * TK + Lv * rs) / (cp + (Lv * Lv * rs * EPS) / (Rd * TK * TK));
  }

  // Integrasi adiabat basah dari (p1,T1K) ke p2 (p2<p1), N substep di ln p. -> T2 (K).
  function moistStep(p1, T1K, p2) {
    const n = Math.max(1, Math.ceil(Math.abs(Math.log(p1 / p2)) / 0.02));
    const dlnp = (Math.log(p2) - Math.log(p1)) / n;   // negatif (naik)
    let T = T1K, lnp = Math.log(p1);
    for (let i = 0; i < n; i++) {
      const k1 = moistDToDlnp(T, Math.exp(lnp));
      const k2 = moistDToDlnp(T + k1 * dlnp, Math.exp(lnp + dlnp));
      T += 0.5 * (k1 + k2) * dlnp;   // Heun (RK2)
      lnp += dlnp;
    }
    return T;
  }

  /* Hitung profil parcel + indeks. profile = {levels:[hPa surface->atas], T:[°C],
   * RH:[%], u:[m/s], v:[m/s]}. Mengembalikan objek turunan untuk render. */
  function derive(profile) {
    const L = profile.levels, n = L.length;
    const T = profile.T, RH = profile.RH, u = profile.u, v = profile.v;
    const Td = new Array(n), Tv = new Array(n);
    for (let i = 0; i < n; i++) {
      Td[i] = dewpoint(T[i], RH[i]);
      const e = esat(Td[i]);
      Tv[i] = Tvirt(T[i], mixRatio(e, L[i]));
    }

    // Parcel dari level terbawah (permukaan model).
    const p0 = L[0], T0 = T[0], Td0 = Td[0];
    const r0 = mixRatio(esat(Td0), p0);   // rasio campuran parcel (kekal di bawah LCL)
    const lc = lcl(p0, T0, Td0);

    const parcelT = new Array(n), parcelTv = new Array(n);
    let curP = lc.p, curTK = lc.T + K0;   // integrator basah mulai dari LCL
    for (let i = 0; i < n; i++) {
      const p = L[i];
      let TpC;
      if (p >= lc.p) {                       // di bawah/di LCL: adiabat kering
        TpC = (T0 + K0) * Math.pow(p / p0, kappa) - K0;
        parcelTv[i] = Tvirt(TpC, r0);
      } else {                               // di atas LCL: adiabat basah (integrasi berurutan)
        curTK = moistStep(curP, curTK, p);
        curP = p;
        TpC = curTK - K0;
        const rs = mixRatio(esat(TpC), p);
        parcelTv[i] = Tvirt(TpC, rs);
      }
      parcelT[i] = TpC;
    }

    // Buoyancy (K) = Tv_parcel - Tv_env. LFC/EL/CAPE/CIN.
    const b = new Array(n);
    for (let i = 0; i < n; i++) b[i] = parcelTv[i] - Tv[i];

    let lfc = null, el = null, cape = 0, cin = 0;
    // cari LFC: crossing negatif->positif di atas LCL
    let iLcl = 0;
    while (iLcl < n - 1 && L[iLcl] > lc.p) iLcl++;   // indeks pertama di/atas LCL
    for (let i = Math.max(1, iLcl); i < n; i++) {
      if (b[i] > 0 && b[i - 1] <= 0) { lfc = L[i];
        // interpolasi tekanan LFC (linear di ln p thd b)
        const f = b[i - 1] / (b[i - 1] - b[i]);
        lfc = Math.exp(Math.log(L[i - 1]) + f * (Math.log(L[i]) - Math.log(L[i - 1])));
        break;
      }
    }
    if (lfc !== null) {
      for (let i = 1; i < n; i++) {
        if (L[i] <= lfc && b[i] < 0 && b[i - 1] >= 0) {
          const f = b[i - 1] / (b[i - 1] - b[i]);
          el = Math.exp(Math.log(L[i - 1]) + f * (Math.log(L[i]) - Math.log(L[i - 1])));
          break;
        }
      }
    }
    // CAPE/CIN via integrasi Rd * b * dln p per lapis
    for (let i = 1; i < n; i++) {
      const dlnp = Math.log(L[i - 1] / L[i]);     // >0 (naik)
      const bb = 0.5 * (b[i] + b[i - 1]);
      const pmid = 0.5 * (L[i] + L[i - 1]);
      if (lfc !== null && pmid <= lfc && (el === null || pmid >= el)) {
        if (bb > 0) cape += Rd * bb * dlnp;         // area positif LFC..EL
      } else if (pmid > (lfc || 0)) {
        if (bb < 0) cin += Rd * bb * dlnp;          // area negatif bawah LFC
      }
    }

    // Lifted Index (500 hPa): Tenv - Tparcel
    let li = null;
    const i500 = L.indexOf(500);
    if (i500 >= 0) li = T[i500] - parcelT[i500];

    return {
      levels: L, T, Td, u, v, parcelT,
      lcl: lc, lfc, el, cape: Math.max(0, cape), cin, li,
    };
  }

  // =====================================================================
  //  RENDER SVG (Skew-T log-P klasik)
  // =====================================================================
  const PTOP = 100, PBOT = 1050;         // rentang tekanan diagram (hPa)
  const TMIN = -40, TMAX = 40;           // rentang suhu di dasar (°C)

  function makeGeom(W, H, pad) {
    const x0 = pad.l, y0 = pad.t, plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;
    const yBot = y0 + plotH;
    const lnTop = Math.log(PTOP), lnBot = Math.log(PBOT);
    const yOf = (p) => y0 + (Math.log(p) - lnTop) / (lnBot - lnTop) * plotH;
    const SKEW = plotW * 0.9;            // pergeseran-x total dari bawah ke atas (miring)
    const xOf = (Tc, p) => x0 + (Tc - TMIN) / (TMAX - TMIN) * plotW + (yBot - yOf(p)) / plotH * SKEW;
    return { x0, y0, plotW, plotH, yBot, yOf, xOf };
  }

  function poly(pts, stroke, w, dash, fill) {
    const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    return `<path d="${d}" fill="${fill || "none"}" stroke="${stroke}" stroke-width="${w}"${dash ? ` stroke-dasharray="${dash}"` : ""}/>`;
  }

  // sampel kurva sepanjang tekanan (untuk adiabat / garis rasio campuran)
  function curve(fn, g, pLo, pHi) {
    const pts = [];
    for (let p = pHi; p >= pLo - 1; p -= 25) {
      const Tc = fn(p);
      if (Tc === null || !isFinite(Tc)) continue;
      pts.push([g.xOf(Tc, p), g.yOf(p)]);
    }
    return pts;
  }

  // wind barb (kt) di posisi x,y; u,v m/s. bearing meteorologi.
  function barb(x, y, u, v) {
    const spdKt = Math.sqrt(u * u + v * v) * MS_TO_KT;
    if (spdKt < 2.5) return `<circle cx="${x}" cy="${y}" r="3" fill="none" stroke="#333" stroke-width="1"/>`;
    const ang = Math.atan2(-u, -v);            // arah DARI mana angin datang
    const dx = Math.sin(ang), dy = -Math.cos(ang);
    const L = 26;                              // panjang batang
    const bx = x - dx * L, by = y - dy * L;    // ujung batang (arah angin datang)
    let s = `<line x1="${x}" y1="${y}" x2="${bx.toFixed(1)}" y2="${by.toFixed(1)}" stroke="#222" stroke-width="1.2"/>`;
    // duri: 50=bendera,10=penuh,5=setengah
    let rem = Math.round(spdKt / 5) * 5;
    const px = -dx, py = -dy;                   // arah sepanjang batang (ke ujung)
    const perpx = -dy, perpy = dx;              // tegak lurus
    let pos = 0;                                // 0..L dari titik pangkal
    const step = 4.5;
    const put = (len, half, flag) => {
      const ax = x + px * (L - pos), ay = y + py * (L - pos);
      if (flag) {
        const b1x = ax + px * step, b1y = ay + py * step;
        const tx = ax + perpx * 10, ty = ay + perpy * 10;
        s += `<path d="M${ax.toFixed(1)} ${ay.toFixed(1)} L${tx.toFixed(1)} ${ty.toFixed(1)} L${b1x.toFixed(1)} ${b1y.toFixed(1)} Z" fill="#222"/>`;
        pos += step * 1.4;
      } else {
        const ln = half ? 5 : 10;
        const tx = ax + perpx * ln + px * 3, ty = ay + perpy * ln + py * 3;
        s += `<line x1="${ax.toFixed(1)}" y1="${ay.toFixed(1)}" x2="${tx.toFixed(1)}" y2="${ty.toFixed(1)}" stroke="#222" stroke-width="1.2"/>`;
        pos += step;
      }
    };
    while (rem >= 50) { put(0, false, true); rem -= 50; }
    while (rem >= 10) { put(0, false, false); rem -= 10; }
    if (rem >= 5) put(0, true, false);
    return s;
  }

  /* Render diagram. d = hasil derive(). opts {W,H}. Kembalikan string SVG. */
  function svg(d, opts) {
    opts = opts || {};
    const W = opts.W || 340, H = opts.H || 380;
    const pad = { l: 34, r: 34, t: 12, b: 34 };
    const g = makeGeom(W, H, pad);
    const parts = [];
    const clip = `<clipPath id="skclip"><rect x="${g.x0}" y="${g.y0}" width="${g.plotW}" height="${g.plotH}"/></clipPath>`;

    // --- latar: isoterm miring ---
    let bg = "";
    for (let Tc = -100; Tc <= 60; Tc += 10) {
      const p1 = [g.xOf(Tc, PBOT), g.yOf(PBOT)], p2 = [g.xOf(Tc, PTOP), g.yOf(PTOP)];
      const zero = Tc === 0;
      bg += poly([p1, p2], zero ? "#9fb0c8" : "#e2e6ee", zero ? 1.1 : 0.8);
    }
    // --- adiabat kering (theta const) ---
    for (let th = -30; th <= 160; th += 10) {
      const fn = (p) => (th + K0) * Math.pow(p / 1000, kappa) - K0;
      bg += poly(curve(fn, g, PTOP, PBOT), "#f0d9b8", 0.7);
    }
    // --- adiabat basah (dari T di 1000 hPa) ---
    for (let Tb = -20; Tb <= 40; Tb += 5) {
      const pts = [];
      let TK = Tb + K0, p = 1000;
      for (; p >= PTOP - 1; p -= 25) {
        pts.push([g.xOf(TK - K0, p), g.yOf(p)]);
        const np = p - 25; if (np < PTOP - 1) break;
        TK = moistStep(p, TK, np);
      }
      bg += poly(pts, "#bfe3c8", 0.7, "1 3");
    }
    // --- garis rasio campuran (g/kg), dashed ---
    [1, 2, 4, 8, 12, 16, 20].forEach((rgkg) => {
      const r = rgkg / 1000;
      const fn = (p) => {
        const e = r * p / (EPS + r);
        const ln = Math.log(Math.max(e, 1e-6) / 6.112);
        return 243.5 * ln / (17.67 - ln);
      };
      bg += poly(curve(fn, g, 400, PBOT), "#cfe0b0", 0.7, "2 3");
    });

    // --- sumbu ---
    let axis = "";
    [1000, 850, 700, 500, 400, 300, 250, 200, 150, 100].forEach((p) => {
      const y = g.yOf(p);
      axis += `<line x1="${g.x0}" y1="${y.toFixed(1)}" x2="${g.x0 + g.plotW}" y2="${y.toFixed(1)}" stroke="#eceef2" stroke-width="0.6"/>`;
      axis += `<text x="${g.x0 - 4}" y="${(y + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="#5a6472" font-family="monospace">${p}</text>`;
    });
    for (let Tc = -40; Tc <= 40; Tc += 20) {
      const x = g.xOf(Tc, PBOT);
      axis += `<text x="${x.toFixed(1)}" y="${(g.yBot + 14).toFixed(1)}" text-anchor="middle" font-size="9" fill="#5a6472" font-family="monospace">${Tc}</text>`;
    }
    axis += `<text x="${(g.x0 - 24).toFixed(1)}" y="${(g.y0 + g.plotH / 2).toFixed(1)}" text-anchor="middle" font-size="9" fill="#8a94a6" font-family="monospace" transform="rotate(-90 ${(g.x0 - 24).toFixed(1)} ${(g.y0 + g.plotH / 2).toFixed(1)})">hPa</text>`;
    axis += `<text x="${(g.x0 + g.plotW / 2).toFixed(1)}" y="${(g.yBot + 27).toFixed(1)}" text-anchor="middle" font-size="9" fill="#8a94a6" font-family="monospace">Suhu (°C)</text>`;

    // --- arsir CAPE (merah) & CIN (biru): antara parcel & lingkungan ---
    let shade = "";
    const Ld = d.levels;
    function areaBetween(iA, iB, color) {
      const up = [], dn = [];
      for (let i = iA; i <= iB; i++) { up.push([g.xOf(d.parcelT[i], Ld[i]), g.yOf(Ld[i])]); dn.push([g.xOf(d.T[i], Ld[i]), g.yOf(Ld[i])]); }
      const pts = up.concat(dn.reverse());
      shade += poly(pts, "none", 0, null, color);
    }
    // tandai indeks lapis untuk CAPE (parcel>env) & CIN (parcel<env)
    for (let i = 1; i < Ld.length; i++) {
      const warm = d.parcelT[i] > d.T[i] && d.parcelT[i - 1] > d.T[i - 1];
      const cold = d.parcelT[i] < d.T[i] && d.parcelT[i - 1] < d.T[i - 1];
      const inCape = d.lfc && Ld[i] <= d.lfc && (!d.el || Ld[i] >= d.el);
      if (warm && inCape) areaBetween(i - 1, i, "rgba(226,35,32,0.22)");
      else if (cold && d.lfc && Ld[i] > d.lfc) areaBetween(i - 1, i, "rgba(35,96,200,0.18)");
    }

    // --- kurva T (merah), Td (hijau), parcel (putus-putus) ---
    const tPts = Ld.map((p, i) => [g.xOf(d.T[i], p), g.yOf(p)]);
    const tdPts = Ld.map((p, i) => [g.xOf(d.Td[i], p), g.yOf(p)]);
    const paPts = Ld.map((p, i) => [g.xOf(d.parcelT[i], p), g.yOf(p)]);
    let lines = poly(paPts, "#131b2e", 1.3, "4 3");
    lines += poly(tdPts, "#1f8a4c", 2, null);
    lines += poly(tPts, "#e42320", 2, null);

    // --- penanda LCL / LFC / EL ---
    let marks = "";
    const mark = (p, label, color) => {
      if (!p) return;
      const y = g.yOf(p);
      marks += `<line x1="${g.x0}" y1="${y.toFixed(1)}" x2="${g.x0 + g.plotW}" y2="${y.toFixed(1)}" stroke="${color}" stroke-width="1" stroke-dasharray="5 3"/>`;
      marks += `<text x="${g.x0 + 3}" y="${(y - 3).toFixed(1)}" text-anchor="start" font-size="9" font-weight="700" fill="${color}" font-family="monospace">${label}</text>`;
    };
    mark(d.lcl.p, "LCL", "#4648d4");
    mark(d.lfc, "LFC", "#d97706");
    mark(d.el, "EL", "#7a1fa2");

    // --- wind barbs (kolom kanan) ---
    let barbs = "";
    const bx = W - pad.r + 16;
    for (let i = 0; i < Ld.length; i++) {
      if (Ld[i] < PTOP) continue;
      // hindari barb terlalu rapat: lewati bila dekat barb sebelumnya
      barbs += barb(bx, g.yOf(Ld[i]), d.u[i], d.v[i]);
    }

    const body =
      `<g clip-path="url(#skclip)">${bg}${shade}${lines}${marks}</g>${axis}` +
      `<rect x="${g.x0}" y="${g.y0}" width="${g.plotW}" height="${g.plotH}" fill="none" stroke="rgba(19,27,46,0.16)" stroke-width="1"/>` +
      `<g>${barbs}</g>`;
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px" xmlns="http://www.w3.org/2000/svg"><defs>${clip}</defs>${body}</svg>`;
  }

  window.SkewT = { derive, svg, dewpoint };
})();
