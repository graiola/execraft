const DEFAULT_SCALE = 1;
const MIN_SCALE = 0.08;
const MAX_SCALE = 1.6;
const ZOOM_STEP = 1.15;
const PAN_STEP = 72;
const DRAG_THRESHOLD = 3;

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function isInteractive(target) {
  return Boolean(
    target.closest(
      "button, a, input, select, textarea, summary, [contenteditable='true']",
    ),
  );
}

/**
 * Owns navigation for the workflow's otherwise presentation-only canvas.
 *
 * The controller deliberately knows nothing about workPackages or orchestration.
 * It only translates pointer, wheel, keyboard, and toolbar input into a stable
 * scroll/scale viewport, keeping graph rendering independently testable.
 */
export class WorkflowViewport {
  constructor({ viewport, space, canvas, controls = null, output = null, onChange = null }) {
    if (!viewport || !space || !canvas) {
      throw new Error("WorkflowViewport requires viewport, space, and canvas");
    }
    this.viewport = viewport;
    this.space = space;
    this.canvas = canvas;
    this.controls = controls;
    this.output = output;
    this.onChange = typeof onChange === "function" ? onChange : null;
    this.scale = DEFAULT_SCALE;
    this.drag = null;
    this.wheelDelta = 0;
    this.wheelPoint = null;
    this.wheelFrame = 0;
    this.resizeObserver = null;

    this.handlePointerDown = (event) => this.#pointerDown(event);
    this.handlePointerMove = (event) => this.#pointerMove(event);
    this.handlePointerEnd = (event) => this.#pointerEnd(event);
    this.handleWheel = (event) => this.#wheel(event);
    this.handleKeyDown = (event) => this.#keyDown(event);
    this.handleScroll = () => this.#notifyChange();
    this.handleControl = (event) => this.#control(event);
    this.handleResize = () => this.#syncSpace();

    this.viewport.addEventListener("pointerdown", this.handlePointerDown);
    this.viewport.addEventListener("pointermove", this.handlePointerMove);
    this.viewport.addEventListener("pointerup", this.handlePointerEnd);
    this.viewport.addEventListener("pointercancel", this.handlePointerEnd);
    this.viewport.addEventListener("wheel", this.handleWheel, {
      passive: false,
    });
    this.viewport.addEventListener("keydown", this.handleKeyDown);
    this.viewport.addEventListener("scroll", this.handleScroll, { passive: true });
    this.controls?.addEventListener("click", this.handleControl);
    if (typeof ResizeObserver !== "undefined") {
      this.resizeObserver = new ResizeObserver(() => this.#syncSpace());
      this.resizeObserver.observe(this.canvas);
    }
    window.addEventListener("resize", this.handleResize, { passive: true });
    this.#applyScale();
  }

  destroy() {
    cancelAnimationFrame(this.wheelFrame);
    this.resizeObserver?.disconnect();
    this.viewport.removeEventListener("pointerdown", this.handlePointerDown);
    this.viewport.removeEventListener("pointermove", this.handlePointerMove);
    this.viewport.removeEventListener("pointerup", this.handlePointerEnd);
    this.viewport.removeEventListener("pointercancel", this.handlePointerEnd);
    this.viewport.removeEventListener("wheel", this.handleWheel);
    this.viewport.removeEventListener("keydown", this.handleKeyDown);
    this.viewport.removeEventListener("scroll", this.handleScroll);
    this.controls?.removeEventListener("click", this.handleControl);
    window.removeEventListener("resize", this.handleResize);
  }

  snapshot() {
    return Object.freeze({
      x: Math.max(0, this.viewport.scrollLeft),
      y: Math.max(0, this.viewport.scrollTop),
      scale: this.scale,
    });
  }

  restore({ x = 0, y = 0, scale = DEFAULT_SCALE } = {}) {
    this.scale = clamp(Number(scale) || DEFAULT_SCALE, MIN_SCALE, MAX_SCALE);
    this.#applyScale();
    this.viewport.scrollTo({
      left: Math.max(0, Number(x) || 0),
      top: Math.max(0, Number(y) || 0),
      behavior: "auto",
    });
    this.#notifyChange();
  }

  setScale(nextScale, { clientX = null, clientY = null } = {}) {
    const scale = clamp(nextScale, MIN_SCALE, MAX_SCALE);
    if (Math.abs(scale - this.scale) < 0.001) return;

    const rect = this.viewport.getBoundingClientRect();
    const anchorX = clientX == null ? rect.width / 2 : clientX - rect.left;
    const anchorY = clientY == null ? rect.height / 2 : clientY - rect.top;
    const contentX = (this.viewport.scrollLeft + anchorX) / this.scale;
    const contentY = (this.viewport.scrollTop + anchorY) / this.scale;

    this.scale = scale;
    this.#applyScale();
    this.viewport.scrollLeft = contentX * scale - anchorX;
    this.viewport.scrollTop = contentY * scale - anchorY;
    this.#notifyChange();
  }

  reset() {
    this.scale = DEFAULT_SCALE;
    this.#applyScale();
    this.viewport.scrollTo({ left: 0, top: 0, behavior: "auto" });
    this.#notifyChange();
  }

  fit() {
    const width = this.canvas.offsetWidth;
    const height = this.canvas.offsetHeight;
    if (!width || !height) return;
    const padding = 24;
    const availableWidth = Math.max(1, this.viewport.clientWidth - padding * 2);
    const availableHeight = Math.max(1, this.viewport.clientHeight - padding * 2);
    this.scale = clamp(
      Math.min(availableWidth / width, availableHeight / height),
      MIN_SCALE,
      DEFAULT_SCALE,
    );
    this.#applyScale();
    this.viewport.scrollLeft = Math.max(
      0,
      (width * this.scale - this.viewport.clientWidth) / 2,
    );
    this.viewport.scrollTop = Math.max(
      0,
      (height * this.scale - this.viewport.clientHeight) / 2,
    );
    this.#notifyChange();
  }

  #pointerDown(event) {
    // Touch remains native so vertical gestures can belong to the page; mouse and pen use grab-to-pan.
    if (event.button !== 0 || event.pointerType === "touch") return;
    if (isInteractive(event.target)) return;
    this.viewport.focus({ preventScroll: true });
    this.viewport.setPointerCapture(event.pointerId);
    this.drag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      lastX: event.clientX,
      lastY: event.clientY,
      moved: false,
    };
  }

  #pointerMove(event) {
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    const totalX = event.clientX - this.drag.startX;
    const totalY = event.clientY - this.drag.startY;
    if (!this.drag.moved && Math.hypot(totalX, totalY) < DRAG_THRESHOLD) {
      return;
    }
    this.drag.moved = true;
    this.viewport.classList.add("is-panning");
    this.viewport.scrollLeft += this.drag.lastX - event.clientX;
    this.viewport.scrollTop += this.drag.lastY - event.clientY;
    this.drag.lastX = event.clientX;
    this.drag.lastY = event.clientY;
    event.preventDefault();
  }

  #pointerEnd(event) {
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    if (this.viewport.hasPointerCapture(event.pointerId)) {
      this.viewport.releasePointerCapture(event.pointerId);
    }
    this.drag = null;
    this.viewport.classList.remove("is-panning");
  }

  #wheel(event) {
    const lineHeight = 16;
    const pageHeight = this.viewport.clientHeight;
    const multiplier =
      event.deltaMode === 1
        ? lineHeight
        : event.deltaMode === 2
          ? pageHeight
          : 1;
    const deltaX = event.deltaX * multiplier;
    const deltaY = event.deltaY * multiplier;

    // Zoom is explicit so a normal page wheel is never trapped unexpectedly.
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      this.wheelDelta += deltaY;
      this.wheelPoint = { clientX: event.clientX, clientY: event.clientY };
      if (this.wheelFrame) return;
      this.wheelFrame = requestAnimationFrame(() => {
        const factor = Math.exp(-this.wheelDelta * 0.0015);
        const point = this.wheelPoint;
        this.wheelDelta = 0;
        this.wheelPoint = null;
        this.wheelFrame = 0;
        this.setScale(this.scale * factor, point || {});
      });
      return;
    }

    // Ordinary vertical wheel/trackpad gestures belong to the document.
    // Shift+wheel is the explicit mouse gesture for horizontal graph panning.
    // Native horizontal trackpad deltas remain available because the viewport
    // keeps horizontal overflow enabled in CSS.
    if (!event.shiftKey) return;

    const horizontalDelta = deltaX || deltaY;
    const maxLeft = Math.max(0, this.viewport.scrollWidth - this.viewport.clientWidth);
    const canScrollX =
      (horizontalDelta < 0 && this.viewport.scrollLeft > 0) ||
      (horizontalDelta > 0 && this.viewport.scrollLeft < maxLeft);
    if (!canScrollX) return;

    event.preventDefault();
    this.viewport.scrollBy({
      left: horizontalDelta,
      top: 0,
      behavior: "auto",
    });
  }

  #keyDown(event) {
    if (event.target !== this.viewport) return;
    const amount = event.shiftKey ? PAN_STEP * 3 : PAN_STEP;
    const panByKey = {
      ArrowLeft: [-amount, 0],
      ArrowRight: [amount, 0],
      ArrowUp: [0, -amount],
      ArrowDown: [0, amount],
    };
    if (panByKey[event.key]) {
      event.preventDefault();
      this.viewport.scrollBy(...panByKey[event.key]);
      return;
    }
    if (["+", "="].includes(event.key)) {
      event.preventDefault();
      this.setScale(this.scale * ZOOM_STEP);
    } else if (event.key === "-") {
      event.preventDefault();
      this.setScale(this.scale / ZOOM_STEP);
    } else if (event.key === "0") {
      event.preventDefault();
      this.reset();
    }
  }

  #control(event) {
    const button = event.target.closest("[data-workflow-viewport-action]");
    if (!button || !this.controls.contains(button)) return;
    const action = button.dataset.workflowViewportAction;
    if (action === "zoom-in") this.setScale(this.scale * ZOOM_STEP);
    else if (action === "zoom-out") this.setScale(this.scale / ZOOM_STEP);
    else if (action === "reset") this.reset();
    else if (action === "fit") this.fit();
    this.viewport.focus({ preventScroll: true });
  }

  #notifyChange() {
    this.onChange?.(this.snapshot());
  }

  #applyScale() {
    this.canvas.style.transform = `scale(${this.scale})`;
    this.#syncSpace();
    const percentage = Math.round(this.scale * 100);
    if (this.output) {
      this.output.value = `${percentage}%`;
      this.output.textContent = `${percentage}%`;
    }
  }

  #syncSpace() {
    const width = Math.max(this.canvas.offsetWidth * this.scale, 1);
    const height = Math.max(this.canvas.offsetHeight * this.scale, 1);
    this.space.style.width = `${width}px`;
    this.space.style.height = `${height}px`;
  }
}
