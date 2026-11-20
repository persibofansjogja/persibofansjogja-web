(() => {
  const list = document.querySelector('#fixture-list');
  const status = document.querySelector('#fixture-status');
  const updated = document.querySelector('#fixture-updated');
  const refresh = document.querySelector('#refresh-fixtures');
  let matches = [], filter = 'all';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function render(){
    const visible = matches.filter(m => filter === 'all' || m.status === filter);
    if(!visible.length){list.innerHTML='<div class="fixture-empty"><span>✳</span><h3>BELUM ADA PERTANDINGAN</h3><p>Tidak ada data untuk kategori ini.</p></div>';return;}
    list.innerHTML=visible.map((m,i)=>{
      const time = m.time || m.kickoff || '';
      const venue = m.venue || m.stadium || '';
      const details = [
        time ? `<span class="fixture-detail"><span aria-hidden="true">◷</span> ${esc(time)} WIB</span>` : '',
        venue ? `<span class="fixture-detail"><span aria-hidden="true">⌖</span> ${esc(venue)}</span>` : ''
      ].filter(Boolean).join('');
      return `<article class="fixture-card"><div class="fixture-number">${String(i+1).padStart(2,'0')}</div><div class="fixture-main"><div class="fixture-meta"><span>${esc(m.date || 'Tanggal belum tersedia')}</span><span class="fixture-status-tag ${m.status==='Selesai'?'done':''}">${esc(m.status || 'Info')}</span></div><div class="fixture-raw">${esc(m.raw)}</div>${details?`<div class="fixture-details">${details}</div>`:''}</div>${m.score?`<div class="fixture-score">${esc(m.score)}</div>`:'<div class="fixture-vs">VS</div>'}</article>`;
    }).join('');
  }
  async function load(){
    status.hidden=false;status.innerHTML='<span class="loading-spinner"></span> Mengambil data dari iLeague…';refresh.disabled=true;
    try{
      const r=await fetch('/api/jadwal-persibo',{headers:{'Accept':'application/json'}});
      if(!r.ok) throw new Error('HTTP '+r.status);
      const data=await r.json();matches=data.matches||[];
      updated.textContent=data.updated?'Diperbarui '+data.updated:'';
      status.hidden=matches.length>0;
      if(data.error&&!matches.length){status.textContent=data.error;}
      render();
    }catch(e){status.textContent='Data pertandingan belum dapat dimuat. Silakan coba lagi atau buka laman resmi iLeague.';list.innerHTML='';status.hidden=false;}
    finally{refresh.disabled=false;}
  }
  document.querySelectorAll('.fixture-filter').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.fixture-filter').forEach(x=>x.classList.toggle('active',x===b));filter=b.dataset.filter;render()}));
  refresh.addEventListener('click',load);load();
})();
