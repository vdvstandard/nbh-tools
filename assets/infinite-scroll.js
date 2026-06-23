(function() {
  const loadMoreBtn = document.querySelector('[data-endless-scroll-button]');
  if (!loadMoreBtn) return;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        loadMoreBtn.click();
      }
    });
  }, { rootMargin: '300px' });

  observer.observe(loadMoreBtn);
  loadMoreBtn.style.visibility = 'hidden';
})();
