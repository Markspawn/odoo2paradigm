const search = document.querySelector('#product-search');
const results = document.querySelector('#product-results');
let controller;
async function findProducts() {
  if (!search || !results) return;
  if (controller) controller.abort();
  controller = new AbortController();
  results.textContent = 'Searching…';
  try {
    const response = await fetch('/api/products?q=' + encodeURIComponent(search.value), {signal: controller.signal});
    if (!response.ok) throw new Error('Search failed. Sign in again if your session expired.');
    const products = await response.json();
    results.replaceChildren();
    if (!products.length) { results.textContent = 'No matches. Try a shorter code or a word from the description.'; return; }
    for (const product of products) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'product-option';
      const code = document.createElement('b'); code.textContent = product.id + ' · ' + product.uom;
      const description = document.createElement('span'); description.textContent = product.description;
      button.append(code, description);
      button.addEventListener('click', () => {
        document.querySelector('#product_id').value = product.id;
        document.querySelector('#selected-description').textContent = product.description;
        document.querySelector('#price_mode').value = product.uom.toUpperCase() === 'LF' ? 'lf' : 'piece';
        results.replaceChildren();
      });
      results.append(button);
    }
  } catch (error) { if (error.name !== 'AbortError') results.textContent = error.message; }
}
document.querySelector('#search-button')?.addEventListener('click', findProducts);
search?.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); findProducts(); } });
document.querySelectorAll('form[data-working]').forEach(form => form.addEventListener('submit', () => {
  const button = form.querySelector('button[type=submit]');
  button.disabled = true; button.textContent = 'Working…';
}));
