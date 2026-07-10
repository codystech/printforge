import * as THREE from 'three';
import { STLLoader } from '/vendor/STLLoader.js';
import { OrbitControls } from '/vendor/OrbitControls.js';

const liveViewers = new Set();
let frameRequested = false;

function scheduleFrame() {
  if (frameRequested) return;
  frameRequested = true;
  requestAnimationFrame(() => {
    frameRequested = false;
    for (const viewer of liveViewers) viewer.renderIfNeeded();
  });
}

function disposeMaterial(material) {
  for (const mat of Array.isArray(material) ? material : [material]) {
    if (!mat) continue;
    for (const key of Object.keys(mat)) {
      const value = mat[key];
      if (value?.isTexture) value.dispose();
    }
    mat.dispose();
  }
}

function roleColor(role) {
  return {
    reference: 0x7188a7,
    fit_cutout: 0x54d7dc,
    negative: 0xff6685,
    assembly: 0xae7cff,
    printable: 0x4ea1ff,
  }[role] ?? 0x4ea1ff;
}

function assetUrl(asset) {
  if (typeof asset === 'string') return asset;
  if (!asset) return null;
  return asset.preview_url ?? asset.stl_url ?? asset.url ?? asset.download_url
    ?? (asset.stl_id ? `/stl/${asset.stl_id}` : null);
}

export class CandidateViewer {
  constructor(container, label) {
    this.container = container;
    this.label = label;
    this.initialized = false;
    this.visible = false;
    this.dirty = false;
    this.destroyed = false;
    this.applyingSync = false;
    this.syncEnabled = true;
    this.partner = null;
    this.radius = 50;
    this.center = new THREE.Vector3();
    this.pending = null;
    this.layers = { reference: true, negative: false, issues: true, wireframe: false };
    this.placeholder = container.querySelector('.viewer-placeholder');
    this.intersectionObserver = new IntersectionObserver(entries => {
      const entry = entries[0];
      this.visible = !!entry?.isIntersecting;
      if (this.visible) {
        this.initialize();
        if (this.pending) this.consumePending();
        this.invalidate();
      }
    }, { rootMargin: '120px' });
    this.intersectionObserver.observe(container);
  }

  initialize() {
    if (this.initialized || this.destroyed) return;
    this.initialized = true;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x090e15);
    this.camera = new THREE.PerspectiveCamera(48, 1, 0.1, 6000);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.domElement.setAttribute('aria-label', `${this.label} interactive 3D preview`);
    this.renderer.domElement.setAttribute('role', 'img');
    this.container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = false;
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener('change', () => this.onControlsChange());

    this.scene.add(new THREE.HemisphereLight(0xf3f8ff, 0x243044, 1.5));
    const key = new THREE.DirectionalLight(0xffffff, 1.8);
    key.position.set(1.2, 1.8, 1.4);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x54d7dc, .65);
    rim.position.set(-1, .5, -1);
    this.scene.add(rim);

    this.world = new THREE.Group();
    this.world.rotation.x = -Math.PI / 2;
    this.scene.add(this.world);
    this.modelGroup = new THREE.Group();
    this.markerGroup = new THREE.Group();
    this.world.add(this.modelGroup, this.markerGroup);
    this.loader = new STLLoader();
    this.drawBed([220, 220]);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.resize();
    liveViewers.add(this);
  }

  setPartner(partner) { this.partner = partner; }
  setSynchronized(enabled) { this.syncEnabled = !!enabled; }

  onControlsChange() {
    this.invalidate();
    if (!this.applyingSync && this.syncEnabled && this.partner?.initialized) {
      this.partner.applyPose(this.capturePose());
    }
  }

  capturePose() {
    const r = Math.max(this.radius, .001);
    return {
      offset: this.camera.position.clone().sub(this.controls.target).divideScalar(r),
      targetOffset: this.controls.target.clone().sub(this.center).divideScalar(r),
      fov: this.camera.fov,
    };
  }

  applyPose(pose) {
    if (!pose || !this.initialized) return;
    this.applyingSync = true;
    this.controls.target.copy(this.center).addScaledVector(pose.targetOffset, this.radius);
    this.camera.position.copy(this.controls.target).addScaledVector(pose.offset, this.radius);
    this.camera.fov = pose.fov;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.applyingSync = false;
    this.invalidate();
  }

  resize() {
    if (!this.initialized) return;
    const width = Math.max(1, this.container.clientWidth);
    const height = Math.max(1, this.container.clientHeight);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    this.invalidate();
  }

  invalidate() { this.dirty = true; scheduleFrame(); }
  renderIfNeeded() {
    if (!this.initialized || !this.visible || !this.dirty || this.destroyed) return;
    this.renderer.render(this.scene, this.camera);
    this.dirty = false;
  }

  drawBed(bed) {
    if (!this.initialized) return;
    if (this.bedGroup) this.disposeObject(this.bedGroup);
    const [x, y] = Array.isArray(bed) ? bed : [220, 220];
    const group = new THREE.Group();
    group.name = 'build-plate';
    const points = [];
    for (let gx = 0; gx <= x; gx += 20) points.push(new THREE.Vector3(gx, 0, 0), new THREE.Vector3(gx, y, 0));
    for (let gy = 0; gy <= y; gy += 20) points.push(new THREE.Vector3(0, gy, 0), new THREE.Vector3(x, gy, 0));
    group.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: 0x233143, transparent: true, opacity: .7 })));
    const outline = [[0, 0], [x, 0], [x, y], [0, y]].map(([px, py]) => new THREE.Vector3(px, py, 0));
    group.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(outline), new THREE.LineBasicMaterial({ color: 0x52657d })));
    group.position.z = -.04;
    this.world.add(group);
    this.bedGroup = group;
    this.invalidate();
  }

  load(assets, options = {}) {
    const list = Array.isArray(assets) ? assets : assets ? [assets] : [];
    this.pending = { assets: list, options };
    if (this.initialized && this.visible) this.consumePending();
  }

  loadDemo67(variant, options = {}) {
    this.pending = { assets: [], options, demoVariant: variant === 'B' ? 'B' : 'A' };
    if (this.initialized && this.visible) this.consumePending();
  }

  // Single-flight, latest-wins loader. Concurrent load() calls only queue this.pending; one runner
  // drains to the newest request. This replaced a Symbol-token scheme where a superseded run bailed
  // silently at a token check and left the pane stuck on "Loading persisted geometry…" forever.
  async consumePending() {
    if (this._loading || !this.initialized) return;
    this._loading = true;
    try {
      while (this.pending) {
        const request = this.pending;
        this.pending = null;
        await this.renderRequest(request);
      }
    } finally {
      this._loading = false;
    }
  }

  async renderRequest(request) {
    this.clearModels();
    this.setMessage('Loading persisted geometry…');
    if (request.options.bed) this.drawBed(request.options.bed);
    if (request.demoVariant) {
      this.buildDemo67(request.demoVariant);
      this.fit();
      this.setIssues(request.options.issues ?? []);
      this.setMessage('');
      this.addBadge('DEMO PROCEDURAL PREVIEW · NOT GENERATED GEOMETRY');
      this.invalidate();
      return;
    }
    if (!request.assets.length) { this.setMessage('No persisted geometry yet'); this.invalidate(); return; }
    try {
      const loaded = [];
      for (const asset of request.assets) {
        const url = assetUrl(asset);
        if (!url || !/\.stl(?:$|\?)/i.test(url) && !asset?.stl_id && !url.startsWith('/stl/')) continue;
        const response = await fetch(url, { cache: 'force-cache' });
        if (!response.ok) throw new Error(`preview ${response.status}`);
        const geometry = this.loader.parse(await response.arrayBuffer());
        if (this.pending) { geometry.dispose(); return; } // newer request queued — drain loop renders it
        geometry.computeVertexNormals();
        const role = asset?.role ?? 'printable';
        const material = new THREE.MeshStandardMaterial({ color: roleColor(role), roughness: .52, metalness: .06, transparent: role !== 'printable' && role !== 'assembly', opacity: role === 'negative' ? .24 : role === 'reference' || role === 'fit_cutout' ? .46 : 1 });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.userData.role = role;
        mesh.userData.asset = asset;
        this.modelGroup.add(mesh);
        loaded.push(mesh);
      }
      if (!loaded.length) throw new Error('No supported STL preview artifact is available');
      this.applyLayers();
      this.fit();
      this.setIssues(request.options.issues ?? []);
      this.setMessage('');
      this.addBadge(`${loaded.length} persisted asset${loaded.length === 1 ? '' : 's'}`);
    } catch (error) {
      this.setMessage(`Preview unavailable — ${error.message}`);
    }
    this.invalidate();
  }

  buildDemo67(variant) {
    const bodyMaterial = new THREE.MeshStandardMaterial({ color: 0x28394f, roughness: .58, metalness: .04 });
    const accentMaterial = new THREE.MeshStandardMaterial({ color: variant === 'A' ? 0x4ea1ff : 0xae7cff, roughness: .42, metalness: .08 });
    const detailMaterial = new THREE.MeshStandardMaterial({ color: 0xe7eef7, roughness: .5 });
    const add = (geometry, material, position, rotation = null) => {
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(...position);
      if (rotation) mesh.rotation.set(...rotation);
      mesh.userData.role = 'printable';
      this.modelGroup.add(mesh);
      return mesh;
    };
    add(new THREE.BoxGeometry(76, 44, 8), bodyMaterial, [38, 22, 4]);
    // Spinner: A visibly increases radial clearance; B adds a retention cap.
    const spinnerRadius = variant === 'A' ? 10.8 : 10.2;
    add(new THREE.TorusGeometry(spinnerRadius, 2.4, 14, 36), accentMaterial, [22, 22, 10]);
    add(new THREE.CylinderGeometry(3.6, 3.6, 5, 24), detailMaterial, [22, 22, 10], [Math.PI / 2, 0, 0]);
    if (variant === 'B') add(new THREE.CylinderGeometry(5.2, 5.2, 1.5, 28), accentMaterial, [22, 22, 13], [Math.PI / 2, 0, 0]);
    // Captured slider is deliberately identical in both variants.
    add(new THREE.BoxGeometry(26, 5.5, 2), detailMaterial, [51, 22, 9]);
    add(new THREE.BoxGeometry(8, 12, 4), accentMaterial, [49, 22, 11]);
    // Geometric 6 / 7 glyphs stand in for the preserved SIX SEVEN marking.
    add(new THREE.TorusGeometry(4.2, 1.1, 8, 24), detailMaterial, [59, 11, 9.3]);
    add(new THREE.BoxGeometry(2, 7, 1.8), detailMaterial, [55.8, 8.8, 9.3]);
    add(new THREE.BoxGeometry(9, 1.8, 1.8), detailMaterial, [69, 7.5, 9.3]);
    add(new THREE.BoxGeometry(1.8, 10, 1.8), detailMaterial, [66, 12, 9.3], [0, 0, -.45]);
    this.applyLayers();
    this.invalidate();
  }

  setMessage(message) {
    if (this.placeholder) {
      this.placeholder.textContent = message;
      this.placeholder.hidden = !message;
    }
  }

  addBadge(text) {
    this.container.querySelector('.viewer-badge')?.remove();
    if (!text) return;
    const badge = document.createElement('span');
    badge.className = 'viewer-badge';
    badge.textContent = text;
    this.container.appendChild(badge);
  }

  clearModels() {
    if (!this.initialized) return;
    for (const child of [...this.modelGroup.children]) this.disposeObject(child);
    for (const child of [...this.markerGroup.children]) this.disposeObject(child);
    this.container.querySelector('.viewer-badge')?.remove();
  }

  setIssues(issues) {
    if (!this.initialized) return;
    for (const child of [...this.markerGroup.children]) this.disposeObject(child);
    const geometry = new THREE.SphereGeometry(1.8, 12, 8);
    let used = false;
    for (const issue of issues) {
      const point = issue.coordinates ?? issue.coordinate ?? issue.position;
      if (!Array.isArray(point) || point.length < 3 || point.some(v => !Number.isFinite(Number(v)))) continue;
      const material = new THREE.MeshBasicMaterial({ color: /critical|error/i.test(issue.severity ?? '') ? 0xff4f60 : 0xf0ad52, depthTest: false });
      const marker = new THREE.Mesh(geometry, material);
      marker.position.set(...point.slice(0, 3).map(Number));
      marker.userData.issue = issue;
      marker.renderOrder = 20;
      this.markerGroup.add(marker);
      used = true;
    }
    if (!used) geometry.dispose();
    this.applyLayers();
    this.invalidate();
  }

  focusIssue(issue) {
    if (!this.initialized) return;
    const point = issue?.coordinates ?? issue?.coordinate ?? issue?.position;
    if (!Array.isArray(point) || point.length < 3) return;
    const target = this.world.localToWorld(new THREE.Vector3(...point.slice(0, 3).map(Number)));
    const direction = this.camera.position.clone().sub(this.controls.target).normalize();
    this.controls.target.copy(target);
    this.camera.position.copy(target).addScaledVector(direction, Math.max(18, this.radius * .25));
    this.controls.update();
    this.invalidate();
  }

  setLayer(name, enabled) {
    this.layers[name] = !!enabled;
    this.applyLayers();
  }

  applyLayers() {
    if (!this.initialized) return;
    this.modelGroup.traverse(object => {
      if (!object.isMesh) return;
      const role = object.userData.role;
      object.visible = role === 'reference' || role === 'fit_cutout' ? this.layers.reference : role === 'negative' ? this.layers.negative : true;
      object.material.wireframe = this.layers.wireframe;
    });
    this.markerGroup.visible = this.layers.issues;
    this.invalidate();
  }

  fit() {
    if (!this.initialized || !this.modelGroup.children.length) return;
    this.modelGroup.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(this.modelGroup);
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    this.center.copy(sphere.center);
    this.radius = Math.max(sphere.radius, 15);
    this.controls.target.copy(this.center);
    this.camera.position.copy(this.center).add(new THREE.Vector3(1.4, 1.05, 1.4).multiplyScalar(this.radius));
    this.camera.near = Math.max(.05, this.radius / 500);
    this.camera.far = Math.max(2000, this.radius * 25);
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.invalidate();
  }

  preset(name) {
    if (!this.initialized) return;
    const direction = { iso: [1.4, 1.05, 1.4], top: [0, 2.4, .001], front: [0, .15, 2.4], side: [2.4, .15, 0] }[name] ?? [1.4, 1.05, 1.4];
    this.controls.target.copy(this.center);
    this.camera.position.copy(this.center).add(new THREE.Vector3(...direction).multiplyScalar(this.radius));
    this.controls.update();
    this.invalidate();
  }

  disposeObject(object) {
    object.parent?.remove(object);
    object.traverse?.(child => {
      child.geometry?.dispose();
      if (child.material) disposeMaterial(child.material);
    });
  }

  dispose() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.intersectionObserver?.disconnect();
    if (!this.initialized) return;
    liveViewers.delete(this);
    this.resizeObserver?.disconnect();
    this.controls?.dispose();
    this.clearModels();
    if (this.bedGroup) this.disposeObject(this.bedGroup);
    this.renderer?.dispose();
    this.renderer?.forceContextLoss();
    this.renderer?.domElement.remove();
  }
}
