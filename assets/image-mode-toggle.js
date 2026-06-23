class ImageModeToggle {
  static storageKey = 'neighborhood-image-mode';

  static currentMode = 'product';

  static init() {
    this.handleClick = this.handleClick.bind(this);
    this.handleProductGridUpdated = this.handleProductGridUpdated.bind(this);
    this.handleVariantChange = this.handleVariantChange.bind(this);

    document.addEventListener('click', this.handleClick);
    document.addEventListener('product-grid:updated', this.handleProductGridUpdated);

    if (typeof subscribe === 'function' && typeof PUB_SUB_EVENTS !== 'undefined' && PUB_SUB_EVENTS.variantChange) {
      subscribe(PUB_SUB_EVENTS.variantChange, this.handleVariantChange);
    }

    this.sync();
  }

  static getMode() {
    try {
      const storedMode = sessionStorage.getItem(this.storageKey);
      if (storedMode === 'product' || storedMode === 'model') return storedMode;
    } catch (error) {
      return this.currentMode || 'product';
    }

    return this.currentMode || 'product';
  }

  static setMode(mode) {
    if (mode !== 'product' && mode !== 'model') return;

    this.currentMode = mode;
    document.documentElement.dataset.imageMode = mode;

    try {
      sessionStorage.setItem(this.storageKey, mode);
    } catch (error) {
      // Ignore browsers where sessionStorage is unavailable.
    }

    this.sync();

    document.dispatchEvent(
      new CustomEvent('image-mode:changed', {
        detail: {
          mode,
        },
      })
    );
  }

  static sync() {
    this.currentMode = this.getMode();
    document.documentElement.dataset.imageMode = this.currentMode;
    this.syncToggles();
    this.syncProductGalleries();
  }

  static syncToggles() {
    document.querySelectorAll('[data-image-mode-toggle]').forEach((toggle) => {
      toggle.querySelectorAll('[data-image-mode-option]').forEach((button) => {
        const isActive = button.dataset.imageModeOption === this.currentMode;
        button.classList.toggle('is-active', isActive);
        button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
      });
    });
  }

  static syncProductGalleries() {
    document.querySelectorAll('media-gallery[data-image-mode-enabled]').forEach((gallery) => {
      this.applyModeToGallery(gallery);
    });
  }

  static applyModeToGallery(gallery) {
    if (!gallery || typeof gallery.setActiveMedia !== 'function') return;

    const productMediaId = gallery.dataset.imageModeProductMediaId;
    const modelMediaId = gallery.dataset.imageModeModelMediaId;
    const targetMediaId = this.currentMode === 'model' ? modelMediaId || productMediaId : productMediaId || modelMediaId;

    if (!targetMediaId) return;

    const activeMediaId = gallery.querySelector('.product__media-item.is-active')?.dataset.mediaId;
    if (activeMediaId === targetMediaId) {
      gallery.dataset.imageModeLastApplied = targetMediaId;
      return;
    }

    const shouldPrepend = !gallery.dataset.desktopLayout?.includes('thumbnail');
    gallery.dataset.imageModeLastApplied = targetMediaId;
    gallery.setActiveMedia(targetMediaId, shouldPrepend);
  }

  static handleClick(event) {
    const toggleButton = event.target.closest('[data-image-mode-option]');
    if (!toggleButton) return;

    event.preventDefault();
    this.setMode(toggleButton.dataset.imageModeOption);
  }

  static handleProductGridUpdated() {
    this.syncToggles();
  }

  static handleVariantChange(event) {
    if (!event?.data?.sectionId) return;

    window.requestAnimationFrame(() => {
      this.syncProductGalleries();
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    ImageModeToggle.init();
  });
} else {
  ImageModeToggle.init();
}
