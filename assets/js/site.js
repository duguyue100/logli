(function () {
  'use strict';

  var feed = document.getElementById('feed');
  var search = document.getElementById('search');
  var sortBtn = document.getElementById('sort');
  var noResults = document.getElementById('no-results');
  var reversed = false;

  // KaTeX
  if (window.renderMathInElement) {
    renderMathInElement(document.body, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false }
      ],
      throwOnError: false
    });
  }

  // Live search: filter rendered cards by text match.
  search.addEventListener('input', function () {
    var q = search.value.trim().toLowerCase();
    var visible = 0;
    feed.querySelectorAll('.card').forEach(function (card) {
      var hit = !q || card.textContent.toLowerCase().indexOf(q) !== -1;
      card.classList.toggle('hidden', !hit);
      if (hit) visible++;
    });
    noResults.hidden = visible !== 0;
  });

  // Sort: toggle between newest-first (default) and oldest-first.
  sortBtn.addEventListener('click', function () {
    reversed = !reversed;
    var cards = Array.prototype.slice.call(feed.querySelectorAll('.card'));
    cards.reverse();
    cards.forEach(function (c) { feed.appendChild(c); });
    sortBtn.classList.toggle('reversed', reversed);
    sortBtn.setAttribute('aria-pressed', reversed ? 'true' : 'false');
    sortBtn.title = reversed ? 'Sort: newest first' : 'Sort: oldest first';
    sortBtn.setAttribute('aria-label', sortBtn.title);
  });

  // Share: copy a self-contained HTML snippet for the card.
  feed.addEventListener('click', function (e) {
    var btn = e.target.closest('.share');
    if (!btn) return;
    var card = btn.closest('.card');
    var date = card.querySelector('.card-date').textContent;
    var body = card.querySelector('.card-body').innerHTML;
    var snippet =
      '<div style="max-width:40rem;margin:1.5rem auto;padding:1rem 1.5rem;' +
      'border:1px solid #e5e5e5;border-radius:8px;' +
      'font-family:Georgia,\'Times New Roman\',serif;line-height:1.6;color:#222">' +
      '<div style="font-size:0.85rem;color:#888;margin-bottom:0.5rem">' + date + ' · Logli</div>' +
      body +
      '<div style="font-size:0.75rem;color:#888;margin-top:0.75rem">' +
      'CC BY 4.0 · <a href="https://creativecommons.org/licenses/by/4.0/">license</a></div>' +
      '</div>';
    copyText(snippet).then(function () {
      btn.classList.add('copied');
      btn.title = 'Copied!';
      setTimeout(function () {
        btn.classList.remove('copied');
        btn.title = 'Share';
      }, 1500);
    });
  });

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        resolve();
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(ta);
      }
    });
  }
})();