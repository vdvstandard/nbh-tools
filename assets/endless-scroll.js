class EndlessScroll {
  static instances = new Map();
  static autoLoadRootMargin = '0px 0px 150px 0px';
  static revealAnimationClasses = [
    'scroll-trigger',
    'scroll-trigger--offscreen',
    'animate--slide-in',
    'animate--fade-in',
    'scroll-trigger--cancel',
  ];

  static init(root = document) {
    const endlessScrollElements = root.querySelectorAll('[data-endless-scroll]');

    endlessScrollElements.forEach((element) => {
      if (EndlessScroll.instances.has(element)) return;
      EndlessScroll.instances.set(element, new EndlessScroll(element));
    });

    EndlessScroll.instances.forEach((instance, element) => {
      if (document.body.contains(element)) return;
      instance.destroy();
      EndlessScroll.instances.delete(element);
    });
  }

  constructor(element) {
    this.element = element;
    this.area = this.element.closest('[data-endless-scroll-area]');
    this.gridContainer = this.element.closest('#ProductGridContainer');
    this.grid = this.gridContainer?.querySelector('[data-endless-scroll-grid]');
    this.button = this.element.querySelector('[data-endless-scroll-button]');
    this.loadingText = this.element.dataset.loadingText || 'Loading...';
    this.sectionId = this.element.dataset.sectionId;
    this.nextUrl = this.element.dataset.nextUrl;
    this.isLoading = false;
    this.hasActivatedHybridMode = false;
    this.isButtonIntersecting = false;
    this.observer = null;
    this.onClick = this.onClick.bind(this);
    this.onIntersection = this.onIntersection.bind(this);

    if (!this.area || !this.gridContainer || !this.grid || !this.button || !this.nextUrl) {
      this.element.hidden = true;
      return;
    }

    this.paginationWrapper = this.area.querySelector('.pagination-wrapper');
    this.area.classList.remove('is-fallback-visible');
    this.area.classList.add('endless-scroll-enabled');
    this.button.addEventListener('click', this.onClick);
    this.observe();
  }

  onClick() {
    this.loadNextPage();
  }

  observe() {
    if (this.observer) this.observer.disconnect();

    this.observer = new IntersectionObserver(this.onIntersection, {
      rootMargin: EndlessScroll.autoLoadRootMargin,
    });

    this.observer.observe(this.button);
  }

  onIntersection(entries) {
    entries.forEach((entry) => {
      const wasButtonIntersecting = this.isButtonIntersecting;
      this.isButtonIntersecting = entry.isIntersecting;

      if (!this.hasActivatedHybridMode || this.isLoading || !this.nextUrl) return;
      if (!this.isButtonIntersecting || wasButtonIntersecting) return;
      this.loadNextPage();
    });
  }

  stripRevealAnimation(element) {
    const animatedElements = [
      element,
      ...element.querySelectorAll(
        '.scroll-trigger, .scroll-trigger--offscreen, .scroll-trigger--cancel, .animate--slide-in, .animate--fade-in, [data-cascade], [style*="--animation-order"]'
      ),
    ];

    animatedElements.forEach((animatedElement) => {
      animatedElement.classList.remove(...EndlessScroll.revealAnimationClasses);
      animatedElement.removeAttribute('data-cascade');
      animatedElement.style.removeProperty('--animation-order');
      if (!animatedElement.getAttribute('style')) animatedElement.removeAttribute('style');
    });
  }

  async loadNextPage() {
    if (this.isLoading || !this.nextUrl) return;

    this.isLoading = true;
    this.element.setAttribute('aria-busy', 'true');
    this.element.setAttribute('aria-label', this.loadingText);
    this.button.disabled = true;

    try {
      const requestUrl = new URL(this.nextUrl, window.location.origin);
      if (this.sectionId) requestUrl.searchParams.set('section_id', this.sectionId);

      const response = await fetch(requestUrl.toString());
      if (!response.ok) throw new Error(`Failed to fetch page ${requestUrl.toString()}`);

      const responseText = await response.text();
      const responseHTML = new DOMParser().parseFromString(responseText, 'text/html');
      const nextGrid = responseHTML.querySelector('[data-endless-scroll-grid]');
      const nextItems = nextGrid ? Array.from(nextGrid.children).filter((item) => item.matches('.grid__item')) : [];

      if (nextItems.length === 0) {
        this.area?.classList.add('is-fallback-visible');
        this.stopAutoLoading(true);
        return;
      }

      const fragment = document.createDocumentFragment();
      nextItems.forEach((item) => {
        const nextItem = document.importNode(item, true);
        this.stripRevealAnimation(nextItem);
        fragment.appendChild(nextItem);
      });
      this.grid.appendChild(fragment);

      const nextArea = responseHTML.querySelector('[data-endless-scroll-area]');
      const nextPaginationWrapper = nextArea?.querySelector('.pagination-wrapper');
      if (nextPaginationWrapper && this.paginationWrapper) {
        this.paginationWrapper.replaceWith(document.importNode(nextPaginationWrapper, true));
        this.paginationWrapper = this.area.querySelector('.pagination-wrapper');
      }

      const nextControl = responseHTML.querySelector('[data-endless-scroll]');
      this.nextUrl = nextControl?.dataset.nextUrl || '';
      this.element.dataset.nextUrl = this.nextUrl;

      if (!this.nextUrl) {
        this.stopAutoLoading(true);
        return;
      }

      this.hasActivatedHybridMode = true;
    } catch (error) {
      console.error('Endless scroll failed', error);
      this.area?.classList.add('is-fallback-visible');
      this.stopAutoLoading(true);
      return;
    } finally {
      this.isLoading = false;
      this.button.disabled = false;
      this.element.removeAttribute('aria-busy');
      this.element.removeAttribute('aria-label');
    }
  }

  stopAutoLoading(hideControl) {
    this.button?.removeEventListener('click', this.onClick);
    if (this.observer) this.observer.disconnect();
    this.observer = null;
    this.nextUrl = '';
    this.element.dataset.nextUrl = '';
    if (hideControl) this.element.hidden = true;
  }

  destroy() {
    this.button?.removeEventListener('click', this.onClick);
    if (this.observer) this.observer.disconnect();
    this.observer = null;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  EndlessScroll.init();
});

document.addEventListener('product-grid:updated', (event) => {
  EndlessScroll.init(event.detail?.container || document);
});
