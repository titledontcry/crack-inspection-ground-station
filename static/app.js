const $ = s => document.querySelector(s);
let rows = [], startedAt = Date.now();
const displayTime = v => v ? new Date(v).toLocaleString('th-TH') : '—';
function measure(m) {
  if (!m) return 'กำลังรอผล…'; if (!m.crack_pixels) return 'ไม่พบรอยร้าว';
  const l = m.length_mm != null ? `${(m.length_mm / 1000).toFixed(2)} m` : `${m.length_px.toFixed(0)} px`;
  const w = m.width_mm_max != null ? `${m.width_mm_max.toFixed(2)} mm` : `${m.width_px_max.toFixed(1)} px`;
  return `L ${l} · max ${w}`;
}
function v(value, suffix='—') { return value == null ? suffix : value; }
function telemetry(metadata = {}) {
  const m = metadata; $('#speed-value').textContent=v(m.speed_mps != null ? `${(m.speed_mps*3.6).toFixed(1)} km/h` : null);
  $('#height-value').textContent=v(m.height_m != null ? `${m.height_m.toFixed(1)} m` : null);
  $('#lens-value').textContent=v(m.focal_length_mm != null ? `${m.focal_length_mm} mm` : null);
  $('#iso-value').textContent=v(m.iso); $('#distance-value').textContent=v(m.distance_to_surface_m != null ? `${m.distance_to_surface_m.toFixed(1)} m` : null);
  if (m.battery_pct != null) { $('#battery-value').textContent=`${m.battery_pct}%`; $('#battery-detail').textContent=m.battery_voltage_v ? `${m.battery_voltage_v.toFixed(1)} V` : 'From flight telemetry'; $('#battery-bar').style.width=`${Math.max(0,Math.min(100,m.battery_pct))}%`; }
}
function render(data) {
  rows=data; $('#count').textContent=`${data.length} ภาพ`; telemetry(data[0]?.metadata);
  $('#rows').innerHTML=data.map((r,i)=>`<tr class="${r.status}" data-id="${r.id}"><td>${data.length-i}</td><td><span class="badge">${r.status}</span></td><td>${displayTime(r.captured_at||r.received_at)}</td><td>${r.filename}</td><td><img class="thumb" src="${r.original_url}" alt="original"></td><td>${r.overlay_url?`<img class="thumb" src="${r.overlay_url}" alt="prediction">`:'—'}</td><td>${r.status==='failed'?`ผิดพลาด: ${r.error}`:measure(r.measurement)}</td></tr>`).join('');
  document.querySelectorAll('tr[data-id]').forEach(el=>el.onclick=()=>detail(rows.find(r=>r.id===el.dataset.id)));
}
function detail(r) { $('#detail-title').textContent=r.filename; $('#detail-original').src=r.original_url; $('#detail-overlay').src=r.overlay_url||''; $('#detail-data').textContent=JSON.stringify({metadata:r.metadata,measurement:r.measurement,error:r.error},null,2); $('#detail').showModal(); }
async function refresh(){try{render(await(await fetch('/api/images')).json());const h=await(await fetch('/api/health')).json();$('#health').textContent=h.model_error?`MODEL ERROR`:h.model_loaded?'● SYSTEM READY':'○ MODEL STANDBY';}catch{$('#health').textContent='● OFFLINE';}}
async function setup(){const s=await(await fetch('/api/settings')).json();if(s.live_stream_url){$('#live-image').src=s.live_stream_url;$('#live-empty').hidden=true;$('#live-label').textContent='● STREAM CONNECTED';}refresh();setInterval(refresh,3000);setInterval(()=>{$('#clock').textContent=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});const e=Math.floor((Date.now()-startedAt)/1000);$('#flight-time').textContent=`${String(Math.floor(e/60)).padStart(2,'0')}:${String(e%60).padStart(2,'0')}`},1000);}
$('#close').onclick=()=>$('#detail').close();setup();
$('#demo-button').onclick=async e=>{const b=e.currentTarget;b.disabled=true;b.textContent='QUEUEING…';try{const r=await fetch('/api/demo/test-set?count=5',{method:'POST'});if(!r.ok)throw new Error(await r.text());await refresh();}catch(err){alert(`ไม่สามารถสร้าง demo ได้: ${err.message}`)}finally{b.disabled=false;b.textContent='DEMO 5 TEST IMAGES'}};
