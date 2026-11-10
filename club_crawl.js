// ---- full crawl of the client's site: tree, page-by-page fixes, active recommendations --------
const cPath = u => { try { const x = new URL(u); return decodeURIComponent(x.pathname + x.search); } catch(e){ return u; } };
const TAX = /\/(secteur|domaine|categorie-produit|category|tag)\//;
const LEGAL = /mentions|confidentialit|cgv|reglement|cookies/;
const DECO = /[\u{1D400}-\u{1D7FF}]/u;
function pageFlags(p, cr){
  const f = [], year = Number((cr.checked||"").slice(0,4)) || new Date().getFullYear();
  if (p.status!==200) return [[p.status ? `erreur ${p.status}` : "injoignable", "bad"]];
  if (p.final && p.final!==p.url) return [[`doublon de ${cPath(p.final)}`, "warn"]];
  if (p.url!==cr.home && !p.inbound) f.push(["orpheline", "bad"]);
  else if (p.url!==cr.home && p.inbound===1 && !TAX.test(p.url)) f.push(["1 seul lien entrant", "warn"]);
  if (!p.in_sitemap) f.push(["hors plan du site", "warn"]);
  if (!(p.h1||[]).filter(Boolean).length) f.push(["sans H1", "bad"]); else if (p.h1.length>1) f.push([`${p.h1.length} H1`, "warn"]);
  if (!p.desc) f.push(["sans description", TAX.test(p.url) ? "warn" : "bad"]); else if (p.desc_auto) f.push(["description automatique", "warn"]);
  if (DECO.test(cPath(p.url))) f.push(["adresse illisible", "warn"]);
  if (TAX.test(p.url)) f.push(["archive indexable", "warn"]);
  else if (p.uwords!=null && p.uwords<150 && !LEGAL.test(p.url)) f.push(["contenu court", "warn"]);
  if (p.modified && Number(p.modified.slice(0,4)) < year-1 && !LEGAL.test(p.url)) f.push([`modifiée en ${p.modified.slice(0,4)}`, "warn"]);
  return f;
}
function crawlTree(cr){
  const P = cr.pages||[], by = Object.fromEntries(P.map(p=>[p.url,p])), used = new Set([cr.home]);
  const take = f => { const r = P.filter(p=>!used.has(p.url) && f(p)); r.forEach(p=>used.add(p.url)); return r; };
  const n = s => s.replace(/\/$/, ""), pn = u => { try { return new URL(u).pathname; } catch(e){ return u; } };
  const root = u => pn(u).split("/").filter(Boolean).length<=1 && !new URL(u).search;
  const tl = cr.template_links||[];
  const menu = (cr.menu||[]).filter(([,h])=>h!==cr.home);
  const groups = menu.map(([label, href])=>{
    const page = by[href];
    if (!page) return {label, ext: href, kids: []};
    used.add(href);
    return {label, page, kids: take(p=>!TAX.test(p.url) && p.url!==href && n(pn(p.url)).startsWith(n(pn(href))) && (pn(p.url).length>pn(href).length || p.final===href))};
  });
  groups.forEach(g=>{ if (g.page) g.kids.push(...take(p=>root(p.url) && p.status===200 && (g.page.to||[]).includes(p.url) && !tl.includes(p.url))); });
  const off = take(p=>p.status===200 && !TAX.test(p.url) && !tl.includes(p.url) && root(p.url));
  const foot = take(p=>tl.includes(p.url));
  const tax = take(p=>TAX.test(p.url));
  const dead = take(p=>p.status!==200);
  const rest = take(()=>true);
  const title = p => (p.title||"").replace(/\s*[-–|]\s*LH Sport Club\s*$/i, "");
  const node = p => { const fl = pageFlags(p, cr), s = p.status!==200 || fl.some(x=>x[1]==="bad") ? "bad" : fl.length ? "warn" : "ok";
    return `<span class="nd">${stIcon(s)}<a class="pth" href="${esc(p.url)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(cPath(p.url))}</a>
      <span class="meta">${p.status===200 ? `${esc(title(p).slice(0,70))} · ${fmt(p.uwords ?? p.words)} mots propres · ${p.inbound} lien${p.inbound>1?"s":""} entrant${p.inbound>1?"s":""} · profondeur ${p.depth ?? "—"}` : `liée depuis ${p.inbound} page${p.inbound>1?"s":""}`}</span>
      ${fl.map(x=>`<span class="fl ${x[1]==="bad"?"bad":""}">${esc(x[0])}</span>`).join("")}</span>`; };
  const list = ps => ps.length ? `<ul>${ps.map(p=>`<li>${node(p)}</li>`).join("")}</ul>` : "";
  const grp = (label, ps, open, note) => ps.length ? `<li><details ${open?"open":""}><summary><span class="grp">${label}</span> <span class="meta">${ps.length} page${ps.length>1?"s":""}${note ? " · "+note : ""}</span></summary>${list(ps)}</details></li>` : "";
  const taxBy = {}; tax.forEach(p=>{ const k = pn(p.url).split("/")[1]; (taxBy[k] = taxBy[k] || []).push(p); });
  return `<div class="tree"><ul><li>${node(by[cr.home] || {url:cr.home, status:200, inbound:0})}<ul>
    <li class="meta" style="margin:6px 0 2px">Menu principal</li>
    ${groups.map(g=>g.ext ? `<li><span class="nd">${stIcon("warn")}<b>${esc(g.label)}</b> <span class="meta">→ ${esc(g.ext.replace(/^https?:\/\//,"").slice(0,60))}</span><span class="fl">hors du site</span></span></li>`
      : `<li><details ${g.kids.length<=20?"open":""}><summary>${node(g.page)} <span class="meta">menu « ${esc(g.label)} »${g.kids.length ? ` · ${g.kids.length} sous-page${g.kids.length>1?"s":""}` : ""}</span></summary>${list(g.kids)}</details></li>`).join("")}
    <li class="meta" style="margin:10px 0 2px">Hors menu</li>
    ${grp("Pages absentes du menu", off, true, "atteintes seulement par des liens dans les pages")}
    ${grp("Pied de page", foot, false)}
    ${Object.keys(taxBy).length ? `<li><details><summary><span class="grp">Archives automatiques (WordPress)</span> <span class="meta">${tax.length} pages</span></summary><ul>
      ${Object.entries(taxBy).map(([k,ps])=>grp("/"+k+"/", ps, false)).join("")}</ul></details></li>` : ""}
    ${grp("Pages disparues encore liées", dead, true)}
    ${grp("Autres adresses", rest, false)}
  </ul></li></ul></div>`;
}
function crawlFixes(cr, a){
  const P = cr.pages||[], ok = P.filter(p=>p.status===200 && (!p.final || p.final===p.url));
  const content = ok.filter(p=>!TAX.test(p.url) && !LEGAL.test(p.url)), pth = p => cPath(p.url);
  const year = Number((cr.checked||"").slice(0,4)) || new Date().getFullYear();
  const L = (a && a.home && a.home.local) || {};
  const miss = [["address","adresse"],["geo","GPS"],["openingHoursSpecification","horaires"],["priceRange","prix"]].filter(([k])=>!L[k]).map(x=>x[1]);
  const noalt = ok.map(p=>p.img_noalt||0), base = noalt.length ? Math.min(...noalt) : 0;
  const ext = (cr.menu||[]).filter(([,h])=>!P.some(p=>p.url===h));
  const R = [
    ["SEO","Liens internes cassés", "bad", "Retirer le lien, ou rediriger l'adresse disparue (301) vers la page la plus proche.", (cr.broken||[]).map(b=>`${cPath(b[0])} → ${cPath(b[1])} (${b[2]||"injoignable"})`)],
    ["SEO","Pages sans aucun lien interne (orphelines)", "bad", "Les lier depuis une page du menu, ou les retirer du site et du plan du site.", (cr.orphans||[]).map(cPath)],
    ["SEO","Pages de contenu atteintes par un seul lien", "warn", "Ajouter un lien depuis le menu, l'accueil ou une page proche.", content.filter(p=>p.inbound===1).map(pth)],
    ["SEO","Entrées du menu qui sortent du site", "warn", "Faire pointer le menu vers une page du site ; garder l'outil externe en bouton.", ext.map(([l,h])=>`${l} → ${h}`)],
    ["SEO","Pages sans H1", "bad", "Un titre H1 par page, qui dit ce qu'est la page et « Le Havre ».", content.filter(p=>!(p.h1||[]).filter(Boolean).length).map(pth)],
    ["SEO","Pages avec plusieurs H1", "warn", "Garder un seul H1, passer les autres en H2.", content.filter(p=>(p.h1||[]).length>1).map(p=>`${pth(p)} (${p.h1.length})`)],
    ["SEO","Descriptions générées automatiquement", "warn", "Écrire une description de 70 à 160 caractères dans All in One SEO.", ok.filter(p=>p.desc_auto).map(p=>`${pth(p)} (${p.desc.length} car.)`)],
    ["SEO","Pages de contenu sans description", "bad", "Écrire une description de 70 à 160 caractères.", content.filter(p=>!p.desc).map(pth)],
    ["SEO","Titres trop longs (plus de 65 caractères)", "warn", "Raccourcir : Google coupe vers 60 caractères.", ok.filter(p=>(p.title||"").length>65).map(p=>`${pth(p)} (${p.title.length})`)],
    ["SEO","Archives automatiques indexables", "warn", "Les passer en « noindex » (All in One SEO > Apparence de la recherche > Taxonomies).", ok.filter(p=>TAX.test(p.url)).map(pth)],
    ["SEO","Même page à deux adresses", "warn", "Rediriger (301) la variante vers l'adresse canonique.", P.filter(p=>p.status===200 && p.final && p.final!==p.url).map(p=>`${pth(p)} → ${cPath(p.final)}`)],
    ["SEO","Adresses en lettres décoratives", "warn", "Renommer l'adresse en texte normal et rediriger l'ancienne (301).", ok.filter(p=>DECO.test(pth(p))).map(pth)],
    ["SEO","Contenu propre très court (moins de 150 mots hors menu et pied de page)", "warn", "Développer la page, ou la fusionner avec une page proche.", content.filter(p=>p.uwords!=null && p.uwords<150).map(p=>`${pth(p)} (${p.uwords} mots)`)],
    ["SEO",`Images sans texte alternatif (en plus des ${base} du gabarit)`, "warn", "Décrire chaque photo dans la médiathèque WordPress.", ok.filter(p=>(p.img_noalt||0)>base).sort((x,y)=>y.img_noalt-x.img_noalt).map(p=>`${pth(p)} (${p.img_noalt-base})`)],
    ["GEO","Fiche établissement incomplète dans les données structurées", "bad", "Ajouter le bloc ExerciseGym (recommandation GEO ci-dessus).", miss.length ? [`manque : ${miss.join(", ")}`] : []],
    ["GEO","Fichier llms.txt absent", "warn", "Publier https://lhsportclub.fr/llms.txt (recommandation GEO ci-dessus).", a && a.llms ? [] : ["absent"]],
    ["GEO","Robots IA bloqués dans robots.txt", "bad", "Autoriser les robots des moteurs IA à lire le site.", (cr.robots_blocked||[])],
    ["GEO","Pages non modifiées depuis plus d'un an", "warn", "Mettre à jour et dater : les IA privilégient les sources récentes.", content.filter(p=>p.modified && Number(p.modified.slice(0,4))<year-1).sort((x,y)=>x.modified.localeCompare(y.modified)).map(p=>`${pth(p)} (${fdate(p.modified)})`)],
    ["AEO","Aucune FAQ balisée (FAQPage)", "warn", "Créer la page Questions fréquentes (recommandation AEO ci-dessus).", ok.some(p=>p.faq) ? [] : ["aucune page"]],
    ["AEO","Pages de contenu sans intertitre en forme de question", "warn", "Ajouter 2 ou 3 intertitres qui reprennent les questions des visiteurs.", content.filter(p=>!p.questions).map(pth)],
  ];
  return R.map(([cat,t,s,fix,pages])=>({cat, t, s: pages.length ? s : "ok", fix, pages}));
}
let rPane = "SEO";
function crawlSection(K, cr, a){
  if (!cr) return `<h3>Le site page par page</h3><p class="muted">Lancez <code>python site_crawl.py</code> pour explorer tout le site.</p>`;
  const P = cr.pages||[], ok = P.filter(p=>p.status===200 && (!p.final || p.final===p.url));
  const dead = P.filter(p=>p.status!==200), fixes = crawlFixes(cr, a), recos = K.recos||[];
  const maxd = Math.max(0, ...ok.map(p=>p.depth||0)), own = ok.reduce((t,p)=>t+(p.uwords||0),0);
  window._paste = [];
  const paste = x => { window._paste.push(x.t); return `<div class="paste"><span class="muted" style="font-size:12px">${esc(x.l)}</span><button class="pin" data-copy="${window._paste.length-1}" type="button">Copier</button><pre>${esc(x.t)}</pre></div>`; };
  const card = r => `<div class="reco"><div style="display:flex;gap:6px;margin:0 0 4px;flex-wrap:wrap"><span class="tag" style="background:var(${r.p===1?"--lv5":r.p===2?"--lv4":"--lv3"})">Priorité ${r.p}</span><span class="tag">Impact : ${esc(r.impact)}</span><span class="tag">Temps : ${esc(r.effort)}</span></div>
    <h4>${esc(r.t)}</h4><p class="why muted">${esc(r.why)}</p>
    <b style="font-size:13px">À faire</b><ol class="pl">${r.steps.map(s=>`<li>${esc(s)}</li>`).join("")}</ol>
    ${(r.pages||[]).length ? `<div style="font-size:12px;margin-top:6px"><span class="muted">Pages concernées : </span>${[...new Set(r.pages)].map(u=>link(u, cPath(u))).join(" · ")}</div>` : ""}
    ${(r.paste||[]).map(paste).join("")}</div>`;
  const fixRows = cat => fixes.filter(f=>f.cat===cat).map(f=>`<div class="chk">${stIcon(f.s)}<div style="flex:1"><b style="font-size:13px">${esc(f.t)}${f.pages.length ? " : "+f.pages.length : ""}</b>
    <div class="muted" style="font-size:12px">${f.s==="ok" ? "Rien à corriger." : esc(f.fix)}</div>
    ${f.pages.length ? `<details><summary class="muted" style="font-size:12px;cursor:pointer">voir le détail</summary><div class="muted" style="font-size:12px;word-break:break-all">${f.pages.map(esc).join("<br>")}</div></details>` : ""}</div></div>`).join("");
  const nFix = cat => fixes.filter(f=>f.cat===cat && f.s!=="ok").length;
  return `
  <h3 data-nav="Le site page par page">Le site page par page</h3>
  <div class="card"><b>Arborescence de ${esc(K.site.replace(/^https?:\/\//,"").replace(/\/$/,""))}</b> <span class="muted">— exploration complète du ${esc(fdate(cr.checked))} : plan du site + tous les liens internes suivis</span>
   <div class="kp">
    <div><b>${fmt(ok.length)}</b><span>pages en ligne (${fmt(cr.sitemap_n)} dans le plan du site)</span></div>
    <div><b class="${dead.length?"down":""}">${fmt((cr.broken||[]).length)}</b><span>liens internes cassés, vers ${dead.length} adresses disparues</span></div>
    <div><b class="${(cr.orphans||[]).length?"down":""}">${fmt((cr.orphans||[]).length)}</b><span>pages orphelines (aucun lien ne mène à elles)</span></div>
    <div><b>${fmt(own)}</b><span>mots propres au total, hors menu, pied de page et avis répétés (${fmt(cr.words_total)} avec)</span></div>
    <div><b>${maxd}</b><span>clics au plus depuis l'accueil</span></div>
   </div>
   <p class="note">Chaque ligne : adresse · titre · mots propres à la page · nombre de pages qui y mènent · nombre de clics depuis l'accueil. ✓ rien à signaler · ! à améliorer · ✗ à corriger. Cliquez un groupe pour l'ouvrir ou le fermer.</p>
   ${crawlTree(cr)}</div>

  <h3 data-nav="Recommandations">Recommandations actives</h3>
  <p class="muted" style="margin:0 0 8px">Tirées de la lecture des ${ok.length} pages. SEO, GEO et AEO : quoi faire sur le site, avec le texte ou le code prêt à coller. « Avis et tendances » et « Marketing » : ce qui se passe hors du site. En dessous, les correctifs page par page sont recalculés à chaque collecte : une ligne passe au vert quand c'est corrigé.</p>
  <div class="seg" id="rPane" role="group" aria-label="Volet des recommandations" style="margin-bottom:10px;flex-wrap:wrap">${["SEO","GEO","AEO"].map(p=>`<button data-p="${p}" aria-pressed="${p===rPane}">${p} · ${recos.filter(r=>r.cat===p).length}</button>`).join("")}${K.market ? `<button data-p="AVIS" aria-pressed="${rPane==="AVIS"}">Avis et tendances</button>` : ""}${K.playbook ? `<button data-p="MKT" aria-pressed="${rPane==="MKT"}">Marketing : ce que font les meilleurs</button>` : ""}${K.bing ? `<button data-p="BING" aria-pressed="${rPane==="BING"}">Bing Webmaster Tools</button>` : ""}</div>
  ${K.market ? `<div class="rpane" data-p="AVIS" data-nav="Avis et tendances" ${rPane==="AVIS" ? "" : "hidden"}>${marketSection(K)}</div>` : ""}
  ${K.playbook ? `<div class="rpane" data-p="MKT" data-nav="Marketing" ${rPane==="MKT" ? "" : "hidden"}>${playbookSection(K)}</div>` : ""}
  ${K.bing ? `<div class="rpane" data-p="BING" data-nav="Bing Webmaster" ${rPane==="BING" ? "" : "hidden"}>${bingSection(K)}</div>` : ""}
  ${["SEO","GEO","AEO"].map(p=>`<div class="rpane" data-p="${p}" ${p===rPane ? "" : "hidden"}>
    ${recos.filter(r=>r.cat===p).sort((x,y)=>x.p-y.p).map(card).join("")}
    <div class="card"><b>Correctifs page par page — ${p}</b> <span class="muted">(${nFix(p)} point${nFix(p)>1?"s":""} à traiter, mis à jour à chaque collecte)</span>${fixRows(p)}</div></div>`).join("")}`;
}
function bindCrawl(){
  const seg = document.getElementById("rPane");
  if (seg) seg.onclick = e=>{ const b = e.target.closest("button"); if (!b) return; rPane = b.dataset.p;
    seg.querySelectorAll("button").forEach(x=>x.setAttribute("aria-pressed", x===b));
    document.querySelectorAll(".rpane").forEach(d=>d.hidden = d.dataset.p!==rPane); };
  document.querySelectorAll("[data-copy]").forEach(b=>b.onclick = async ()=>{
    try { await navigator.clipboard.writeText(window._paste[+b.dataset.copy]); b.textContent = "Copié ✓"; }
    catch(e){ const r = document.createRange(); r.selectNodeContents(b.nextElementSibling); const s = getSelection(); s.removeAllRanges(); s.addRange(r); b.textContent = "Sélectionné"; }
    setTimeout(()=>b.textContent = "Copier", 1500); });
}

// ---- competitors' reviews and search trends (club.json "market") -------------------------------
function marketSection(K){
  const M = K.market; if (!M) return "";
  const pct = (a, b) => b ? `${a>=b?"+":""}${Math.round(100*(a/b-1))} %` : "—";
  const lvl = v => v>=15 ? "--lv5" : v>=8 ? "--lv4" : v>=4 ? "--lv3" : v>=2 ? "--lv2" : "--lv1";
  const ST2 = {proved:["ok","Atout prouvé par les avis"], verify:["warn","Atout à confirmer avec le club"], unknown:["info","Pas de donnée sur le club"]};
  const gRows = M.gyms.map(g=>`<tr class="${g.me?"me":""}"><td>${g.url ? link(g.url, g.name) : esc(g.name)}</td>
    <td class="n">${g.rating.toFixed(1).replace(".",",")}</td><td class="n">${fmt(g.count)}</td>
    <td class="n"><span class="tag" style="background:var(${lvl(g.low)})">${String(g.low).replace(".",",")} %</span></td>
    <td style="white-space:normal;min-width:220px;font-size:12px">${g.topics.map(t=>`<span class="tag">${esc(t[0])} ${t[1]}</span>`).join("")}</td>
    <td style="white-space:normal;min-width:260px;font-size:12px">${esc(g.issues || "")}</td></tr>`).join("");
  const theme = t => `<div class="chk">${stIcon(ST2[t.s][0])}<div style="flex:1"><b>${esc(t.t)}</b> <span class="muted" style="font-size:12px">— ${ST2[t.s][1]}</span>
    <div style="font-size:13px;margin-top:3px"><span class="muted">Chez les concurrents :</span> ${esc(t.them)}</div>
    <div style="font-size:13px;margin-top:3px"><span class="muted">Au club :</span> ${esc(t.us)}</div>
    <div style="font-size:13px;margin-top:3px"><b>À écrire sur le site :</b> ${esc(t.say)}</div></div></div>`;
  const T = M.trends, MO = ["janv.","févr.","mars","avr.","mai","juin","juil.","août","sept.","oct.","nov.","déc."];
  const tRows = T.rows.map(r=>`<tr><td><b>${esc(r.kw)}</b></td>
    <td class="n">${r.hn!=null ? String(r.hn).replace(".",",") : "—"}</td>
    <td class="n">${r.hn!=null ? pct(r.hn, r.hn_prev) : `${r.hn_g} %`}</td>
    <td class="n">${r.bn!=null ? pct(r.bn, r.bn_prev) : "—"}</td><td class="n">${pct(r.fr, r.fr_prev)}</td></tr>`).join("");
  const sRows = Object.entries(T.season).map(([k, s])=>{ const mx = Math.max(...s), mn = Math.min(...s);
    return `<tr><td class="f"><b>${esc(k)}</b></td>${s.map(v=>{ const x = (v-mn)/((mx-mn)||1); return `<td style="background:var(${x>.8?"--lv5":x>.6?"--lv4":x>.4?"--lv3":x>.2?"--lv2":"--lv1"})">${v}</td>`; }).join("")}</tr>`; }).join("");
  return `
  <h3 style="margin-top:4px">Ce que reprochent les clients des autres salles</h3>
  <p class="muted" style="margin:0 0 8px">Avis Google Maps de ${M.gyms.length} salles du Havre et de Gonfreville, relevés le ${esc(fdate(M.checked))}, et 168 avis Trustpilot sur les chaînes. Ce que les clients reprochent ailleurs et que le club fait mieux, c'est ce qu'il faut écrire sur le site.</p>
  <div class="card"><b>Les 7 sujets, et où en est le club</b>${M.themes.map(theme).join("")}</div>
  <div class="tw" style="margin:10px 0"><table class="plain"><thead><tr><th>Salle</th><th class="n">Note</th><th class="n">Avis</th><th class="n">1–2 étoiles</th><th>Sujets les plus cités par Google</th><th>Reproches relevés dans les avis</th></tr></thead><tbody>${gRows}</tbody></table></div>
  <p class="note">« 1–2 étoiles » : part des avis à 1 ou 2 étoiles. Sujets : mots que Google repère dans les avis, avec leur nombre (positifs comme négatifs). Reproches : lus dans les avis à 1–3 étoiles que Google affiche en premier (5 par salle sans compte Google).</p>
  <div class="tw" style="margin:10px 0"><table class="plain"><thead><tr><th>Enseigne (Trustpilot, toute la France)</th><th class="n">Note</th><th class="n">Avis</th><th>Reproches principaux (sur 40 à 44 avis récents lus)</th></tr></thead>
    <tbody>${M.brands.map(b=>`<tr><td>${esc(b.b)}</td><td class="n">${esc(b.score)}</td><td class="n">${fmt(b.n)}</td><td style="white-space:normal;min-width:280px;font-size:12px">${esc(b.main)}</td></tr>`).join("")}</tbody></table></div>

  <h3>Ce que les gens cherchent (Google Trends)</h3>
  <div class="card"><b>Évolution sur 12 mois</b> <span class="muted">— moyenne des 52 dernières semaines comparée aux 52 précédentes</span>
   <div class="tw" style="margin:8px 0"><table class="plain"><thead><tr><th>Recherche</th><th class="n">Haute-Normandie (0–100)</th><th class="n">Évolution Haute-Normandie</th><th class="n">Basse-Normandie</th><th class="n">France</th></tr></thead><tbody>${tRows}</tbody></table></div>
   <p class="note">0–100 : intérêt relatif au sein de la même comparaison (« salle de sport » ≈ 50 veut dire environ 2,3 fois plus de recherches qu'HYROX ≈ 22). ${esc(T.regions)}</p>
   <p class="note">${esc(T.weak)}</p></div>
  <div class="card"><b>Saison des recherches</b> <span class="muted">— France, moyenne par mois 2024–2026 ; plus foncé = plus de recherches</span>
   <div class="tw" style="margin:8px 0"><table class="heat"><thead><tr><th></th>${MO.map(m=>`<th>${m}</th>`).join("")}</tr></thead><tbody>${sRows}</tbody></table></div>
   <p class="note">HYROX : pics en octobre-novembre et avril-mai (saisons de courses), creux en juillet et janvier. Pilates et salle de sport : pic à la rentrée (septembre), plus fort qu'en janvier.</p></div>
  <div class="card"><b>Recherches liées</b> <span class="muted">— les plus fréquentes, puis celles en forte hausse</span>
   ${T.related.map(r=>`<div style="margin:6px 0"><b style="font-size:13px">${esc(r.k)}</b><div>${r.top.map(x=>`<span class="tag">${esc(x)}</span>`).join("")}${r.rising.map(x=>`<span class="tag" style="background:var(--lv4)">↑ ${esc(x)}</span>`).join("")}</div></div>`).join("")}</div>
  <div class="card"><b>Pages à écrire, par ordre de priorité</b>
   ${T.pages.map(p=>`<div class="chk"><span class="tag" style="background:var(${p.p===1?"--lv5":p.p===2?"--lv4":"--lv3"})">Priorité ${p.p}</span><div style="flex:1"><b style="font-size:13px">${esc(p.t)}</b>
     <div class="muted" style="font-size:12px">Recherches visées : ${esc(p.kw)}</div><div style="font-size:13px">${esc(p.why)}</div></div></div>`).join("")}</div>`;
}

// ---- what the most successful gyms do (club.json "playbook") ----------------------------------
function playbookSection(K){
  const B = K.playbook; if (!B) return "";
  const host = u => { try { return new URL(u).hostname.replace(/^www\./,""); } catch(e){ return u; } };
  const pcol = p => p===1?"--lv5":p===2?"--lv4":"--lv3";
  return `
  <h3 style="margin-top:4px">Ce que font les salles qui attirent le plus de monde</h3>
  <p class="muted" style="margin:0 0 8px">Recherche du ${esc(fdate(B.checked))}, dans le monde entier. Pour chaque exemple : le chiffre qui prouve le succès, ce qu'ils font, et ce que le club peut reprendre à son échelle.</p>
  <div class="card"><b>Actions pour le club, par ordre de priorité</b>
   ${B.actions.map(a=>`<div class="chk"><span class="tag" style="background:var(${pcol(a.p)})">Priorité ${a.p}</span><div style="flex:1"><b style="font-size:13px">${esc(a.t)}</b>
     <div class="muted" style="font-size:12px">Coût : ${esc(a.cost)} · Temps : ${esc(a.effort)} · Inspiré de : ${esc(a.from)}</div>
     <ol class="pl" style="font-size:13px">${a.steps.map(x=>`<li>${esc(x)}</li>`).join("")}</ol></div></div>`).join("")}</div>
  <div class="card" style="margin-top:10px"><b>Calendrier sur 12 mois</b> <span class="muted">— calé sur les pics de recherche Google et les saisons de courses HYROX</span>
   <div class="tw" style="margin-top:8px"><table class="plain"><tbody>${B.calendar.map(([m,t])=>`<tr><td><b>${esc(m)}</b></td><td style="white-space:normal">${esc(t)}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="card" style="margin-top:10px"><b>Les exemples</b>
   ${B.leaders.map(l=>`<div class="reco" style="margin-top:10px"><h4 style="margin:0">${esc(l.who)}</h4><div class="up" style="font-size:13px;font-weight:600;margin:2px 0 4px">${esc(l.fig)}</div>
     <div style="font-size:13px"><span class="muted">Ce qu'ils font :</span> ${esc(l.what)}</div>
     <div style="font-size:13px;margin-top:4px"><b>Pour LH Sport Club :</b> ${esc(l.lh)}</div>
     <div class="muted" style="font-size:12px;margin-top:4px">Sources : ${l.src.map(u=>link(u, host(u))).join(" · ")}</div></div>`).join("")}
   <p class="note">${esc(B.note)}</p></div>`;
}

// ---- Bing Webmaster Tools: why, step-by-step to-do, planned dashboard (club.json "bing") ------
function bingSection(K){
  const G = K.bing; if (!G) return "";
  const done = G.todo.filter(t=>t.done).length;
  return `
  <h3 style="margin-top:4px">Bing Webmaster Tools : pour être trouvé par ChatGPT et Copilot</h3>
  <p class="muted" style="margin:0 0 8px">Mis à jour le ${esc(fdate(G.checked))}. ${esc(G.intro)}</p>
  <div class="card"><b>Ce que ça apporte</b>
   ${G.why.map(([t,d])=>`<div class="chk"><div><b style="font-size:13px">${esc(t)}</b><div style="font-size:13px">${esc(d)}</div></div></div>`).join("")}</div>
  <div class="card" style="margin-top:10px"><b>À faire, dans l'ordre</b> <span class="muted">— ${done} sur ${G.todo.length} fait${done>1?"s":""}</span>
   ${G.todo.map((t,i)=>`<div class="chk"><span class="tag" style="background:var(${t.done?"--lv1":"--soft"})">${t.done?"Fait":"Étape "+(i+1)}</span><div style="flex:1"><b style="font-size:13px">${esc(t.t)}</b>
     <div class="muted" style="font-size:12px">Qui : ${esc(t.who)} · Temps : ${esc(t.time)}</div>
     <ol class="pl" style="font-size:13px">${t.how.map(x=>`<li>${esc(x)}</li>`).join("")}</ol></div></div>`).join("")}</div>
  <div class="card" style="margin-top:10px"><b>Ce que le tableau affichera</b> <span class="muted">— une fois la clé API enregistrée, relevé chaque lundi</span>
   <div class="tw" style="margin-top:8px"><table class="plain"><thead><tr><th>Donnée</th><th>Appel de l'API</th><th>À quoi ça sert</th></tr></thead><tbody>
   ${G.dash.map(([t,m,d])=>`<tr><td><b>${esc(t)}</b></td><td><code>${esc(m)}</code></td><td style="white-space:normal">${esc(d)}</td></tr>`).join("")}</tbody></table></div>
   <p class="note">${esc(G.dash_note)}</p></div>
  <div class="card" style="margin-top:10px"><b>Liens utiles</b>
   <ul class="pl" style="font-size:13px">${G.links.map(([t,u])=>`<li>${link(u, t)}</li>`).join("")}</ul></div>`;
}
