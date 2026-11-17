/*
 * 차주용 3D 적재 가이드 (Three.js)
 * 백엔드 /api/v1/orders/{id}/packing 의 적재 계획(규칙 + EP 엔진 결과 · 좌표 mm)을 받아
 * 적재함과 상자를 3D 로 그리고, 상차 순서(load_order)대로 한 상자씩 보여 준다.
 *
 * 좌표: 계획 x = 적재함 길이(0 = 운전석 쪽), y = 폭, z = 높이  →  Three.js (X = 길이, Z = 폭, Y = 높이)
 */
(function () {
  "use strict";
  const API = location.origin + "/api/v1";
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 저장 불가 환경 */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* 무시 */ } },
  };
  let token = store.get("baecha_token");

  async function call(path, opts = {}) {
    const r = await fetch(API + path, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) },
    });
    const body = await r.json().catch(() => ({}));
    if (r.status === 401) { logout(); throw new Error("로그인이 필요합니다."); }
    if (!r.ok) throw new Error(body.detail || ("요청 실패 " + r.status));
    return body;
  }

  function show(id) {
    for (const s of ["login", "jobs", "viewer"]) $(s).hidden = s !== id;
    $("logout").hidden = id === "login";
  }

  function logout() {
    token = null; store.del("baecha_token"); stopPlay(); show("login");
  }

  // ------------------------------------------------------------------ 로그인
  $("loginBtn").onclick = async () => {
    $("loginErr").hidden = true;
    try {
      const r = await call("/auth/login", { method: "POST",
        body: JSON.stringify({ email: $("email").value.trim(), password: $("password").value }) });
      if (r.user.role !== "driver") throw new Error("차주 계정으로 로그인해 주세요.");
      token = r.token; store.set("baecha_token", token);
      loadJobs();
    } catch (e) { $("loginErr").textContent = e.message; $("loginErr").hidden = false; }
  };
  $("logout").onclick = async () => { try { await call("/auth/logout", { method: "POST" }); } catch (e) { /* 무시 */ } logout(); };

  // ------------------------------------------------------------------ 화물 목록 (운행 · 제안 · 추천)
  async function loadJobs() {
    show("jobs");
    $("jobList").innerHTML = '<p class="muted">불러오는 중…</p>';
    try {
      const p = await call("/driver/profile");
      $("hello").innerHTML = `<div class="strong">${esc(p.name)} 차주님</div><div class="muted small">${esc(p.truck_type)} · ${esc(p.plate)}</div>`;
      $("state").textContent = p.state;
      const jobs = [];
      if (p.run) jobs.push({ o: p.run, tag: "내 운행" });
      if (p.offer_id) jobs.push({ o: await call("/orders/" + p.offer_id), tag: "배차 제안" });
      if (!p.run) {
        const recs = await call("/driver/recommendations");
        recs.slice(0, 4).forEach((o) => jobs.push({ o, tag: "AI 추천 " + o.rec.rank + "순위" }));
      }
      const withPlan = jobs.filter((j) => j.o.packing);
      if (!withPlan.length) {
        $("jobList").innerHTML = '<div class="card muted">적재 계획이 있는 화물이 없습니다. 배차를 받으면 여기에 나타나요.</div>';
        return;
      }
      $("jobList").innerHTML = "";
      withPlan.forEach(({ o, tag }) => {
        const b = document.createElement("button");
        b.className = "job";
        b.innerHTML = `<div class="t">${esc(o.title)} <span class="pill">${esc(tag)}</span></div>
          <div class="s">${esc(o.id)} · ${esc(o.origin)} → ${esc(o.destination)}</div>
          <div class="s">상자 ${o.packing.placed}개 · ${Math.round(o.packing.loaded_weight).toLocaleString()}kg · 부피 ${Math.round(o.packing.volume_util * 100)}%</div>`;
        b.onclick = () => openViewer(o.id);
        $("jobList").appendChild(b);
      });
      const want = new URLSearchParams(location.search).get("order");
      if (want && withPlan.some((j) => j.o.id === want)) openViewer(want);
    } catch (e) { $("jobList").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  }
  $("back").onclick = () => { stopPlay(); loadJobs(); };

  // ------------------------------------------------------------------ 3D 뷰어
  let renderer, scene, camera, controls, boxes = [], plan = null, step = 0, timer = null;
  const PALETTE = [0x4F46E5, 0x14B8A6, 0xF59E0B, 0x3B82F6, 0x22C55E, 0xEC4899, 0x8B5CF6, 0x0EA5E9];

  async function openViewer(orderId) {
    show("viewer");
    $("stageMsg").hidden = false;
    $("stageMsg").textContent = "적재 계획을 불러오는 중…";
    try {
      plan = await call(`/orders/${orderId}/packing`);
    } catch (e) { $("stageMsg").textContent = e.message; return; }
    if (typeof THREE === "undefined") {
      $("stageMsg").textContent = "3D 라이브러리를 불러오지 못했습니다 (인터넷 연결 확인).";
      return;
    }
    const items = [...plan.items].sort((a, b) => a.load_order - b.load_order);
    plan.sorted = items;
    $("vTitle").textContent = plan.title;
    $("vSub").textContent = `${plan.order_id} · ${plan.container.name}` + (plan.replanned ? " (내 차량 기준 재계산)" : "");
    const m = plan.metrics || {};
    $("metrics").innerHTML = `<div><span class="muted small">상자</span><b>${items.length}개</b></div>
      <div><span class="muted small">부피 적재율</span><b>${Math.round((m.volume_util || 0) * 100)}%</b></div>
      <div><span class="muted small">총 중량</span><b>${Math.round(m.loaded_weight || 0).toLocaleString()}kg</b></div>`;
    build(plan.container, items);
    showChecks(plan);
    $("slider").max = items.length;
    setStep(0);
    $("stageMsg").hidden = true;
  }

  function build(c, items) {
    const stage = $("stage");
    if (!renderer) {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      stage.appendChild(renderer.domElement);
      window.addEventListener("resize", resize);
    }
    scene = new THREE.Scene();
    const L = c.length / 1000, W = c.width / 1000, H = c.height / 1000;
    camera = new THREE.PerspectiveCamera(45, 1, 0.05, 200);
    camera.position.set(L * 1.25, H * 1.9, W * 2.6);
    if (THREE.OrbitControls) {  // 손가락 회전 · 확대 (컨트롤 파일을 못 받으면 고정 시점으로)
      controls = new THREE.OrbitControls(camera, renderer.domElement);
      controls.target.set(L / 2, H / 3, W / 2);
      controls.enableDamping = true;
    } else {
      controls = { update() {} };
      camera.lookAt(L / 2, H / 3, W / 2);
    }
    scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    const sun = new THREE.DirectionalLight(0xffffff, 0.6); sun.position.set(L, H * 3, W * 2); scene.add(sun);
    // 적재함: 바닥 + 테두리 + 운전석 쪽 벽 표시
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(L, W), new THREE.MeshLambertMaterial({ color: 0xD0D5DD }));
    floor.rotation.x = -Math.PI / 2; floor.position.set(L / 2, 0, W / 2); scene.add(floor);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(L, H, W)),
      new THREE.LineBasicMaterial({ color: 0x667085 }));
    edges.position.set(L / 2, H / 2, W / 2); scene.add(edges);
    const cab = new THREE.Mesh(new THREE.PlaneGeometry(W, H), new THREE.MeshBasicMaterial({ color: 0x4F46E5, transparent: true, opacity: 0.12, side: THREE.DoubleSide }));
    cab.rotation.y = Math.PI / 2; cab.position.set(0, H / 2, W / 2); scene.add(cab);
    // 상자
    boxes = items.map((it, i) => {
      const g = new THREE.BoxGeometry(it.length / 1000, it.height / 1000, it.width / 1000);
      const mat = new THREE.MeshLambertMaterial({ color: PALETTE[i % PALETTE.length], transparent: true, opacity: 0.92 });
      const mesh = new THREE.Mesh(g, mat);
      mesh.position.set((it.x + it.length / 2) / 1000, (it.z + it.height / 2) / 1000, (it.y + it.width / 2) / 1000);
      const line = new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineBasicMaterial({ color: 0x0E1116 }));
      mesh.add(line);
      mesh.visible = false;
      scene.add(mesh);
      return mesh;
    });
    resize();
    renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
  }

  // 과적 · 편심 경고: 서버 검증 결과(constraints)의 안전율 · 축중량 · 중량 항목을 빨간/주황 오버레이로
  function showChecks(p) {
    const keys = ["safety", "cog", "weight", "stacking"];
    const cs = (p.constraints || []).filter((c) => keys.includes(c.key));
    const bad = cs.filter((c) => c.status === "fail");
    const warn = cs.filter((c) => c.status === "warn");
    const w = $("warn");
    if (bad.length || warn.length) {
      const top = bad.length ? bad : warn;
      w.className = "warn " + (bad.length ? "fail" : "warnonly");
      w.innerHTML = (bad.length ? "⛔ 적재 금지 — " : "⚠ 주의 — ") + top.map((c) => `${esc(c.name)}: ${esc(c.detail)}`).join("<br>");
      w.hidden = false;
    } else { w.hidden = true; }
    $("checks").innerHTML = cs.map((c) =>
      `<div class="${c.status === "fail" ? "fail" : c.status === "warn" ? "warn2" : ""}">${c.status === "pass" ? "✓" : c.status === "warn" ? "⚠" : "⛔"} ` +
      `<b>${esc(c.name)}</b> · ${esc(c.detail)}</div>`).join("");
    const cog = (p.metrics || {}).cog;
    if (cog && scene) {  // 무게중심 표시 (편심이면 빨간색)
      const axle = cs.find((c) => c.key === "cog");
      const color = axle && axle.status !== "pass" ? 0xD92D20 : 0x12B76A;
      const s = new THREE.Mesh(new THREE.SphereGeometry(0.12, 20, 16), new THREE.MeshBasicMaterial({ color }));
      s.position.set(cog.x / 1000, cog.z / 1000, cog.y / 1000);
      scene.add(s);
    }
  }

  function resize() {
    if (!renderer || !camera) return;
    const s = $("stage");
    renderer.setSize(s.clientWidth, s.clientHeight);
    camera.aspect = s.clientWidth / s.clientHeight;
    camera.updateProjectionMatrix();
  }

  function setStep(n) {
    const items = plan.sorted;
    step = Math.max(0, Math.min(items.length, n));
    $("slider").value = step;
    boxes.forEach((b, i) => {
      b.visible = i < step;
      b.material.opacity = i === step - 1 ? 1 : 0.55;
      b.material.emissive && b.material.emissive.setHex(i === step - 1 ? 0x222222 : 0x000000);
    });
    if (step === 0) {
      $("stepNo").textContent = `0 / ${items.length}`;
      $("stepName").textContent = "적재 전";
      $("stepWhere").textContent = "다음을 눌러 첫 상자부터 확인하세요.";
      return;
    }
    const it = items[step - 1];
    $("stepNo").textContent = `${step} / ${items.length}`;
    $("stepName").textContent = it.name;
    $("stepWhere").textContent = `운전석에서 ${(it.x / 1000).toFixed(2)}m · 왼쪽 벽에서 ${(it.y / 1000).toFixed(2)}m · 높이 ${(it.z / 1000).toFixed(2)}m · `
      + `${it.length}×${it.width}×${it.height}mm · ${it.weight_kg}kg`;
  }

  function stopPlay() { if (timer) { clearInterval(timer); timer = null; $("play").textContent = "자동 재생"; } }
  $("prev").onclick = () => { stopPlay(); setStep(step - 1); };
  $("next").onclick = () => { stopPlay(); setStep(step + 1); };
  $("slider").oninput = (e) => { stopPlay(); setStep(+e.target.value); };
  $("play").onclick = () => {
    if (timer) return stopPlay();
    if (step >= plan.sorted.length) setStep(0);
    $("play").textContent = "멈춤";
    timer = setInterval(() => { if (step >= plan.sorted.length) stopPlay(); else setStep(step + 1); }, 900);
  };

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // 시작
  if (token) loadJobs(); else show("login");
})();
