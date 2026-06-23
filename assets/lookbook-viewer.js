(() => {
  if (window.LookbookViewerManager) {
    window.LookbookViewerManager.sync();
    return;
  }

  const LOOKBOOK_WHEEL_THRESHOLD = 55;
  const LOOKBOOK_NAVIGATION_INTERVAL = 360;

  class LookbookViewer extends HTMLElement {
    static instances = new Set();
    static activeViewer = null;
    static syncFrame = null;

    constructor() {
      super();
      this.activeIndex = 0;
      this.pendingWheelDelta = 0;
      this.wheelRaf = null;
      this.isNavigating = false;
      this.queuedWheelDirection = 0;
      this.navigationTimer = null;
      this.lastWheelDirection = 0;
      this.touchStartY = 0;
      this.touchStartX = 0;
      this.hideStripTimer = null;
      this.raf = null;
      this.isActiveLookbook = false;
      this.isSeasonMenuOpen = false;
      this.hasPrewarmedImages = false;
      this.persistentEventsBound = false;
      this.activeEventsBound = false;

      this.onWheel = this.onWheel.bind(this);
      this.onKeydown = this.onKeydown.bind(this);
      this.onTouchStart = this.onTouchStart.bind(this);
      this.onTouchMove = this.onTouchMove.bind(this);
      this.onTouchEnd = this.onTouchEnd.bind(this);
      this.onPointerMove = this.onPointerMove.bind(this);
      this.onStripEnter = this.onStripEnter.bind(this);
      this.onStripLeave = this.onStripLeave.bind(this);
      this.onResize = this.onResize.bind(this);
      this.onThumbClick = this.onThumbClick.bind(this);
      this.onIndexClick = this.onIndexClick.bind(this);
      this.onCloseClick = this.onCloseClick.bind(this);
      this.onSeasonButtonClick = this.onSeasonButtonClick.bind(this);
      this.onSeasonOptionClick = this.onSeasonOptionClick.bind(this);
      this.onDocumentClick = this.onDocumentClick.bind(this);
    }

    static queueSync() {
      if (this.syncFrame) return;

      this.syncFrame = window.requestAnimationFrame(() => {
        this.syncFrame = null;
        this.sync();
      });
    }

    static sync() {
      const viewers = Array.from(document.querySelectorAll('lookbook-viewer'));
      const switchableViewers = viewers.filter((viewer) => viewer.stage && viewer.slides.length);
      const currentViewer =
        this.activeViewer && viewers.includes(this.activeViewer)
          ? this.activeViewer
          : switchableViewers[0] || viewers[0] || null;

      this.activeViewer = currentViewer;

      viewers.forEach((viewer) => {
        viewer.renderSeasonOptions(switchableViewers.length ? switchableViewers : viewers);
        viewer.setActiveLookbook(viewer === currentViewer);
      });

      this.updateImmersiveState();
    }

    static activate(viewer) {
      if (!viewer || viewer === this.activeViewer) {
        viewer?.closeSeasonMenu();
        return;
      }

      this.activeViewer = viewer;
      this.sync();
    }

    static closeSeasonMenus(exceptViewer = null) {
      this.instances.forEach((viewer) => {
        if (viewer !== exceptViewer) viewer.closeSeasonMenu();
      });
    }

    static updateImmersiveState() {
      const hasActiveViewer = Boolean(this.activeViewer);
      document.documentElement.classList.toggle('lookbook-immersive', hasActiveViewer);
      document.body.classList.toggle('lookbook-immersive', hasActiveViewer);
    }

    connectedCallback() {
      this.setupElements();
      LookbookViewer.instances.add(this);

      if (this.stage && this.slides.length) {
        this.updateHeaderHeight();
        this.bindPersistentEvents();
        this.goTo(0, { immediate: true });
      }

      LookbookViewer.queueSync();
    }

    disconnectedCallback() {
      this.setActiveLookbook(false);
      this.unbindPersistentEvents();
      LookbookViewer.instances.delete(this);

      if (LookbookViewer.activeViewer === this) LookbookViewer.activeViewer = null;
      LookbookViewer.queueSync();
    }

    setupElements() {
      this.sectionId = this.dataset.sectionId;
      this.seasonLabel = this.dataset.lookbookSeasonLabel || 'Lookbook';
      this.stage = this.querySelector('[data-lookbook-stage]');
      this.slides = Array.from(this.querySelectorAll('[data-lookbook-slide]'));
      this.thumbs = Array.from(this.querySelectorAll('[data-lookbook-thumb]'));
      this.mainImages = Array.from(this.querySelectorAll('[data-lookbook-slide] img'));
      this.counter = this.querySelector('[data-lookbook-counter]');
      this.liveRegion = this.querySelector('[data-lookbook-live]');
      this.thumbStrip = this.querySelector('[data-lookbook-strip]');
      this.desktopThumbList = this.thumbStrip ? this.thumbStrip.querySelector('.lookbook-viewer__thumb-list') : null;
      this.drawer = this.querySelector('[data-lookbook-drawer]');
      this.indexButtons = Array.from(this.querySelectorAll('[data-lookbook-index]'));
      this.closeButtons = Array.from(this.querySelectorAll('[data-lookbook-close]'));
      this.seasonMenu = this.querySelector('[data-lookbook-season-menu]');
      this.seasonButton = this.querySelector('[data-lookbook-season-button]');
      this.seasonCurrent = this.querySelector('[data-lookbook-season-current]');
      this.seasonList = this.querySelector('[data-lookbook-season-list]');
    }

    bindPersistentEvents() {
      if (this.persistentEventsBound) return;

      this.thumbs.forEach((thumb) => thumb.addEventListener('click', this.onThumbClick));
      this.indexButtons.forEach((button) => button.addEventListener('click', this.onIndexClick));
      this.closeButtons.forEach((button) => button.addEventListener('click', this.onCloseClick));

      this.seasonButton?.addEventListener('click', this.onSeasonButtonClick);
      this.seasonList?.addEventListener('click', this.onSeasonOptionClick);
      document.addEventListener('click', this.onDocumentClick);

      this.persistentEventsBound = true;
    }

    unbindPersistentEvents() {
      if (!this.persistentEventsBound) return;

      this.thumbs.forEach((thumb) => thumb.removeEventListener('click', this.onThumbClick));
      this.indexButtons.forEach((button) => button.removeEventListener('click', this.onIndexClick));
      this.closeButtons.forEach((button) => button.removeEventListener('click', this.onCloseClick));

      this.seasonButton?.removeEventListener('click', this.onSeasonButtonClick);
      this.seasonList?.removeEventListener('click', this.onSeasonOptionClick);
      document.removeEventListener('click', this.onDocumentClick);

      this.persistentEventsBound = false;
    }

    bindActiveEvents() {
      if (this.activeEventsBound || !this.stage) return;

      this.stage.addEventListener('wheel', this.onWheel, { passive: false });
      this.stage.addEventListener('touchstart', this.onTouchStart, { passive: true });
      this.stage.addEventListener('touchmove', this.onTouchMove, { passive: false });
      this.stage.addEventListener('touchend', this.onTouchEnd, { passive: true });
      this.stage.addEventListener('pointermove', this.onPointerMove);
      window.addEventListener('keydown', this.onKeydown);
      window.addEventListener('resize', this.onResize);

      if (this.thumbStrip) {
        this.thumbStrip.addEventListener('pointerenter', this.onStripEnter);
        this.thumbStrip.addEventListener('pointerleave', this.onStripLeave);
      }

      this.activeEventsBound = true;
    }

    unbindActiveEvents() {
      if (!this.activeEventsBound || !this.stage) return;

      this.stage.removeEventListener('wheel', this.onWheel);
      this.stage.removeEventListener('touchstart', this.onTouchStart);
      this.stage.removeEventListener('touchmove', this.onTouchMove);
      this.stage.removeEventListener('touchend', this.onTouchEnd);
      this.stage.removeEventListener('pointermove', this.onPointerMove);
      window.removeEventListener('keydown', this.onKeydown);
      window.removeEventListener('resize', this.onResize);

      if (this.thumbStrip) {
        this.thumbStrip.removeEventListener('pointerenter', this.onStripEnter);
        this.thumbStrip.removeEventListener('pointerleave', this.onStripLeave);
      }

      this.activeEventsBound = false;
    }

    setActiveLookbook(isActive) {
      this.isActiveLookbook = isActive;
      this.hidden = !isActive;
      this.setAttribute('aria-hidden', isActive ? 'false' : 'true');
      this.dataset.lookbookActive = isActive ? 'true' : 'false';

      if (isActive) {
        this.updateHeaderHeight();
        this.bindActiveEvents();
        this.prewarmImages();
        this.goTo(this.activeIndex, { immediate: true });
        return;
      }

      this.unbindActiveEvents();
      this.closeSeasonMenu();
      this.closeDrawer();
      this.thumbStrip?.classList.remove('is-open');
      this.resetWheelNavigation();
    }

    renderSeasonOptions(viewers) {
      if (!this.seasonButton || !this.seasonCurrent || !this.seasonList) return;

      this.seasonCurrent.textContent = this.seasonLabel;
      this.seasonButton.disabled = viewers.length <= 1;
      this.seasonButton.setAttribute('aria-expanded', 'false');
      this.seasonList.replaceChildren();
      this.seasonList.hidden = true;
      this.seasonMenu?.classList.remove('is-open');
      this.isSeasonMenuOpen = false;

      viewers.forEach((viewer) => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        const isCurrent = viewer === LookbookViewer.activeViewer;

        button.type = 'button';
        button.className = 'lookbook-viewer__season-option';
        button.dataset.lookbookSeasonOption = viewer.sectionId;
        button.setAttribute('role', 'menuitemradio');
        button.setAttribute('aria-checked', isCurrent ? 'true' : 'false');
        button.textContent = viewer.seasonLabel;

        item.appendChild(button);
        this.seasonList.appendChild(item);
      });
    }

    onThumbClick(event) {
      if (!this.isActiveLookbook) return;

      this.resetWheelNavigation();
      this.goTo(Number(event.currentTarget.dataset.lookbookThumb));
      this.closeDrawer();
    }

    onIndexClick() {
      if (!this.isActiveLookbook) return;
      this.openIndex();
    }

    onCloseClick() {
      if (!this.isActiveLookbook) return;
      this.closeDrawer();
    }

    onSeasonButtonClick(event) {
      event.stopPropagation();
      if (!this.isActiveLookbook || this.seasonButton?.disabled) return;

      if (this.isSeasonMenuOpen) {
        this.closeSeasonMenu();
      } else {
        this.openSeasonMenu();
      }
    }

    onSeasonOptionClick(event) {
      const option = event.target.closest('[data-lookbook-season-option]');
      if (!option) return;

      const targetViewer = Array.from(LookbookViewer.instances).find(
        (viewer) => viewer.sectionId === option.dataset.lookbookSeasonOption
      );

      LookbookViewer.activate(targetViewer);
    }

    onDocumentClick(event) {
      if (!this.isSeasonMenuOpen || this.contains(event.target)) return;
      this.closeSeasonMenu();
    }

    openSeasonMenu() {
      LookbookViewer.closeSeasonMenus(this);
      this.closeDrawer();
      this.seasonList.hidden = false;
      this.seasonButton.setAttribute('aria-expanded', 'true');
      this.seasonMenu?.classList.add('is-open');
      this.isSeasonMenuOpen = true;
    }

    closeSeasonMenu() {
      if (!this.seasonList || !this.seasonButton) return;

      this.seasonList.hidden = true;
      this.seasonButton.setAttribute('aria-expanded', 'false');
      this.seasonMenu?.classList.remove('is-open');
      this.isSeasonMenuOpen = false;
    }

    onWheel(event) {
      if (!this.isActiveLookbook) return;
      event.preventDefault();

      const direction = Math.sign(event.deltaY);
      if (direction === 0) return;

      if (direction !== 0 && direction !== this.lastWheelDirection) {
        this.pendingWheelDelta = 0;
        this.queuedWheelDirection = 0;
        this.lastWheelDirection = direction;
      }

      if (this.isNavigating) {
        this.queuedWheelDirection = direction;
        return;
      }

      this.pendingWheelDelta += event.deltaY;

      if (this.wheelRaf) return;

      this.wheelRaf = window.requestAnimationFrame(() => {
        this.wheelRaf = null;
        this.flushWheelNavigation();
      });
    }

    onKeydown(event) {
      if (!this.isConnected || !this.isActiveLookbook) return;

      const nextKeys = ['ArrowDown', 'ArrowRight', 'PageDown'];
      const prevKeys = ['ArrowUp', 'ArrowLeft', 'PageUp'];

      if (nextKeys.includes(event.key)) {
        event.preventDefault();
        this.closeSeasonMenu();
        this.resetWheelNavigation();
        this.goTo(this.activeIndex + 1);
      }

      if (prevKeys.includes(event.key)) {
        event.preventDefault();
        this.closeSeasonMenu();
        this.resetWheelNavigation();
        this.goTo(this.activeIndex - 1);
      }

      if (event.key === 'Escape') {
        if (this.isSeasonMenuOpen) {
          this.closeSeasonMenu();
          return;
        }

        this.closeDrawer();
      }
    }

    onTouchStart(event) {
      const touch = event.changedTouches[0];
      this.touchStartY = touch.clientY;
      this.touchStartX = touch.clientX;
    }

    onTouchMove(event) {
      event.preventDefault();
    }

    onTouchEnd(event) {
      const touch = event.changedTouches[0];
      const deltaY = this.touchStartY - touch.clientY;
      const deltaX = this.touchStartX - touch.clientX;

      if (Math.abs(deltaY) < 48 || Math.abs(deltaY) < Math.abs(deltaX)) return;

      this.closeSeasonMenu();
      this.resetWheelNavigation();
      this.goTo(this.activeIndex + (deltaY > 0 ? 1 : -1));
    }

    onPointerMove(event) {
      if (!window.matchMedia('(min-width: 750px)').matches) return;
      if (this.isSeasonMenuOpen) return;

      const triggerLine = window.innerHeight * 0.75;
      if (event.clientY >= triggerLine) {
        this.openStrip();
      } else {
        this.queueStripClose();
      }
    }

    onStripEnter() {
      clearTimeout(this.hideStripTimer);
      this.openStrip();
    }

    onStripLeave(event) {
      if (event.clientY >= window.innerHeight * 0.75) return;
      this.queueStripClose();
    }

    onResize() {
      if (this.raf) return;

      this.raf = window.requestAnimationFrame(() => {
        this.updateHeaderHeight();
        this.centerActiveDesktopThumb({ immediate: true });
        this.raf = null;
      });
    }

    goTo(index, options = {}) {
      const nextIndex = Math.max(0, Math.min(index, this.slides.length - 1));
      if (nextIndex === this.activeIndex && !options.immediate) return;

      this.activeIndex = nextIndex;
      this.updateSlides();
      this.updateThumbs();
      this.updateCounter();
      this.centerActiveDesktopThumb({ immediate: options.immediate });
    }

    updateSlides() {
      this.slides.forEach((slide, index) => {
        slide.classList.toggle('is-active', index === this.activeIndex);
        slide.setAttribute('aria-hidden', index === this.activeIndex ? 'false' : 'true');
      });
    }

    updateThumbs() {
      this.thumbs.forEach((thumb) => {
        thumb.setAttribute('aria-current', Number(thumb.dataset.lookbookThumb) === this.activeIndex ? 'true' : 'false');
      });
    }

    updateCounter() {
      const text = `${this.formatCounterNumber(this.activeIndex + 1)} | ${this.formatCounterNumber(this.slides.length)}`;
      if (this.counter) this.counter.textContent = text;
      if (this.liveRegion) this.liveRegion.textContent = text;
    }

    formatCounterNumber(number) {
      return String(number).padStart(2, '0');
    }

    centerActiveDesktopThumb(options = {}) {
      if (!this.desktopThumbList || !window.matchMedia('(min-width: 750px)').matches) return;

      const activeThumb = this.thumbStrip.querySelector(`[data-lookbook-thumb="${this.activeIndex}"]`);
      const activeItem = activeThumb ? activeThumb.closest('.lookbook-viewer__thumb-item') : null;
      if (!activeItem) return;

      const targetLeft = activeItem.offsetLeft + activeItem.offsetWidth / 2 - this.desktopThumbList.clientWidth / 2;
      const maxLeft = this.desktopThumbList.scrollWidth - this.desktopThumbList.clientWidth;
      const left = Math.max(0, Math.min(targetLeft, maxLeft));
      const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      this.desktopThumbList.scrollTo({
        left,
        behavior: options.immediate || prefersReducedMotion ? 'auto' : 'smooth',
      });
    }

    flushWheelNavigation() {
      const step = this.getWheelStep(this.pendingWheelDelta);

      if (step === 0) return;

      this.pendingWheelDelta = 0;
      this.navigateBy(step);
    }

    getWheelStep(delta) {
      const absoluteDelta = Math.abs(delta);

      if (absoluteDelta < LOOKBOOK_WHEEL_THRESHOLD) return 0;

      return delta > 0 ? 1 : -1;
    }

    navigateBy(direction) {
      const nextIndex = Math.max(0, Math.min(this.activeIndex + direction, this.slides.length - 1));
      if (nextIndex === this.activeIndex) {
        this.releaseNavigation();
        return;
      }

      this.closeSeasonMenu();
      this.goTo(nextIndex);
      this.startNavigationInterval();
    }

    startNavigationInterval() {
      this.isNavigating = true;
      clearTimeout(this.navigationTimer);

      this.navigationTimer = window.setTimeout(() => {
        this.finishNavigationInterval();
      }, LOOKBOOK_NAVIGATION_INTERVAL);
    }

    finishNavigationInterval() {
      const direction = this.queuedWheelDirection;
      this.queuedWheelDirection = 0;

      if (direction !== 0) {
        this.navigateBy(direction);
        return;
      }

      this.releaseNavigation();
    }

    releaseNavigation() {
      this.isNavigating = false;
      this.pendingWheelDelta = 0;
      clearTimeout(this.navigationTimer);
    }

    resetWheelNavigation() {
      this.queuedWheelDirection = 0;
      this.lastWheelDirection = 0;
      this.releaseNavigation();
    }

    prewarmImages() {
      if (this.hasPrewarmedImages) return;

      this.mainImages.forEach((image) => {
        image.loading = 'eager';

        if (typeof image.decode !== 'function') return;

        image.decode().catch(() => {});
      });

      this.hasPrewarmedImages = true;
    }

    openStrip() {
      clearTimeout(this.hideStripTimer);
      if (this.thumbStrip) this.thumbStrip.classList.add('is-open');
    }

    openIndex() {
      this.closeSeasonMenu();

      if (window.matchMedia('(min-width: 750px)').matches) {
        this.openStrip();
        this.centerActiveDesktopThumb({ immediate: true });
        return;
      }

      this.openDrawer();
    }

    queueStripClose() {
      clearTimeout(this.hideStripTimer);
      this.hideStripTimer = window.setTimeout(() => {
        if (this.thumbStrip) this.thumbStrip.classList.remove('is-open');
      }, 260);
    }

    openDrawer() {
      if (!this.drawer) return;

      this.drawer.classList.add('is-open');
      this.drawer.setAttribute('aria-hidden', 'false');
    }

    closeDrawer() {
      if (!this.drawer) return;

      this.drawer.classList.remove('is-open');
      this.drawer.setAttribute('aria-hidden', 'true');
    }

    updateHeaderHeight() {
      const header = document.querySelector('header');
      const height = header ? Math.round(header.getBoundingClientRect().height) : 0;
      this.style.setProperty('--lookbook-header-height', `${height}px`);
    }
  }

  window.LookbookViewerManager = LookbookViewer;

  if (!customElements.get('lookbook-viewer')) {
    customElements.define('lookbook-viewer', LookbookViewer);
  } else {
    LookbookViewer.sync();
  }

  if (window.Shopify?.designMode) {
    document.addEventListener('shopify:section:select', (event) => {
      const selectedViewer = event.target.querySelector('lookbook-viewer');
      if (selectedViewer) LookbookViewer.activate(selectedViewer);
    });
  }
})();
