const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(__dirname + '/../index.html', 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const payload = JSON.parse(fs.readFileSync(__dirname + '/../data/prices.json', 'utf8'));
payload.last_checked_at = new Date().toISOString();
payload.sources_status = { 'fixture': { status: 'ok', message: 'fixture smoke test' } };
payload.update_summary = { items: payload.items.length, sources_ok: 1, sources_partial: 0, sources_failed: 0 };
const els = {};
function el(id) { return els[id] ||= {id, innerHTML:'', value:'', dataset:{}, handlers:{}, classList:{add(){},remove(){}}, addEventListener(type,fn){this.handlers[type]=fn;}, scrollIntoView(){}}; }
const nav = Array.from({length:8}, (_, i) => ({...el('nav'+i), dataset:{cat:['','Granos','Ganadería','Maquinaria','Fertilizantes','Energía','Tierra','Finanzas'][i]}, classList:{add(){},remove(){}}}));
const document = {documentElement:{dataset:{}}, getElementById:el, querySelectorAll(sel){ return sel==='#nav button' ? nav : []; }};
const sandbox = {document, console, Intl, Date, Number, String, Math, Array, Object, JSON, fetch:async()=>({ok:true,json:async()=>payload})};
(async()=>{
 vm.runInNewContext(js,sandbox);
 await new Promise(resolve=>setTimeout(resolve,30));
 if (!document.documentElement.dataset.agroxLoaded) throw new Error('page not marked loaded');
 if ((el('table').innerHTML.match(/class="row/g)||[]).length < 20) throw new Error('table failed to render');
 if (!el('detail').innerHTML.includes('Equivalencias actuales')) throw new Error('detail failed to render');
 if (!el('live-status').innerHTML.includes('DATOS CONECTADOS')) throw new Error('live state not rendered');
 if (!el('table-head').innerHTML.includes('Máximo 2026')) throw new Error('annual 2026 header not rendered');
 if (!el('table').innerHTML.includes('Dólar oficial')) throw new Error('annual global table not rendered');
 const yearSelect=el('history-range');
 if (!yearSelect.handlers.change) throw new Error('year change handler missing');
 yearSelect.handlers.change({target:{value:'2025'}});
 if (!el('table-head').innerHTML.includes('Máximo 2025')) throw new Error('year selection did not change global annual table');
 if (!el('detail').innerHTML.includes('Extremos del año 2025')) throw new Error('detail did not update annual year');
 console.log('RUNTIME SMOKE OK', {rows:(el('table').innerHTML.match(/class="row/g)||[]).length, year:yearSelect.value, detailChars:el('detail').innerHTML.length});
})().catch(err=>{console.error(err);process.exit(1)});
