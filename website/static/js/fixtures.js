(() => {
  const list = document.querySelector('#fixture-list');
  const status = document.querySelector('#fixture-status');
  const updated = document.querySelector('#fixture-updated');
  const refresh = document.querySelector('#refresh-fixtures');
  const initialData = document.querySelector('#fixture-data');

  if (!list || !status || !refresh) return;

  // The server embeds the archive in the initial HTML. Do not fetch or
  // re-render on startup: the first paint already contains the match cards.
  let matches = [];
  try {
    matches = initialData ? JSON.parse(initialData.textContent || '[]') : [];
    if (!Array.isArray(matches)) matches = [];
  } catch (_) {
    matches = [];
  }

  let filter = 'all';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));

  function render() {
    const visible = matches.filter(m => filter === 'all' || m.status === filter);
    if (!visible.length) {
      list.innerHTML = '<div class="fixture-empty"><span>✳</span><h3>BELUM ADA PERTANDINGAN</h3><p>Tidak ada data untuk kategori ini.</p></div>';
      return;
    }

    list.innerHTML = visible.map((m, i) => {
      const time = m.time || m.kickoff || '';
      const venue = m.venue || m.stadium || '';
      const details = [
        time ? `<span class="fixture-detail"><span aria-hidden="true">◷</span> ${esc(time)} WIB</span>` : '',
        venue ? `<span class="fixture-detail"><span aria-hidden="true">⌖</span> ${esc(venue)}</span>` : ''
      ].filter(Boolean).join('');
      const description = m.raw || m.opponent || 'Informasi pertandingan belum tersedia';

      return `<article class="fixture-card">
        <div class="fixture-number">${String(i + 1).padStart(2, '0')}</div>
        <div class="fixture-main">
          <div class="fixture-meta">
            <span>${esc(m.date || 'Tanggal belum tersedia')}</span>
            <span class="fixture-status-tag ${m.status === 'Selesai' ? 'done' : ''}">${esc(m.status || 'Info')}</span>
          </div>
          <div class="fixture-raw">${esc(description)}</div>
          ${details ? `<div class="fixture-details">${details}</div>` : ''}
        </div>
        ${m.score ? `<div class="fixture-score">${esc(m.score)}</div>` : '<div class="fixture-vs">VS</div>'}
      </article>`;
    }).join('');
  }

  async function load() {
    status.hidden = false;
    status.textContent = 'Memperbarui arsip jadwal dan hasil pertandingan…';
    refresh.disabled = true;

    try {
      const response = await fetch('/api/jadwal-persibo', {
        headers: { 'Accept': 'application/json' }
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const data = await response.json();
      if (!Array.isArray(data.matches)) throw new Error('Format data pertandingan tidak valid');

      matches = data.matches;
      filter = 'all';
      document.querySelectorAll('.fixture-filter').forEach(button => {
        button.classList.toggle('active', button.dataset.filter === 'all');
      });

      if (updated) {
        updated.textContent = data.updated ? `Arsip diperbarui ${data.updated}` : '';
      }

      status.hidden = matches.length > 0;
      if (!matches.length) {
        status.textContent = data.error || 'Belum ada data pertandingan yang tersimpan.';
      }
      render();
    } catch (_) {
      // Keep the server-rendered/current cards visible if refresh fails.
      status.textContent = 'Pembaruan gagal. Data yang sedang ditampilkan tetap dipertahankan.';
      status.hidden = matches.length > 0;
    } finally {
      refresh.disabled = false;
    }
  }

  document.querySelectorAll('.fixture-filter').forEach(button => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.fixture-filter').forEach(item => {
        item.classList.toggle('active', item === button);
      });
      filter = button.dataset.filter || 'all';
      render();
    });
  });

  refresh.addEventListener('click', load);
})();
