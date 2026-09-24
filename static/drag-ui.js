/* SortableJS 1.15.6: original MIT runtime in vendor/sortable.min.js.
   Motion uses Sortable's animation, fallback clone and auto-scroll implementations. */
const sourceExpanded=new Map();
let sourceOrderSaving=false;
function setSourceExpanded(card,open){
 sourceExpanded.set(card.dataset.sourceId,open);card.classList.toggle('source-collapsed',!open);
 card.querySelector('.source-collapse').setAttribute('aria-expanded',String(open));
 card.querySelector('.source-collapse').textContent=open?'收起⌃':'展开⌄';
}
function prepareSourceCard(card,src){
 const title=card.querySelector('.source-title'),body=make('div','source-card-body');
 [...card.children].filter(el=>el!==title).forEach(el=>body.append(el));card.append(body);
 const fold=body.querySelector('details');if(fold){fold.open=true;fold.querySelector('summary')?.remove()}
 const handle=make('button','source-drag-handle','⠿');handle.type='button';handle.title='拖动排序';handle.setAttribute('aria-label',`${src.name} 拖动排序`);title.prepend(handle);
 const toggle=make('button','button ghost source-collapse');toggle.type='button';toggle.onclick=()=>setSourceExpanded(card,card.classList.contains('source-collapsed'));title.append(toggle);
 setSourceExpanded(card,sourceExpanded.get(src.dataset_id)??!!state.sourcesExpanded);
}
function dragOptions(extra={}){return {animation:220,easing:'cubic-bezier(0.2, 0.8, 0.2, 1)',forceFallback:true,fallbackOnBody:true,fallbackTolerance:5,delay:120,delayOnTouchOnly:true,touchStartThreshold:5,ghostClass:'sort-placeholder',chosenClass:'sort-picked',fallbackClass:'sort-floating',scroll:true,scrollSensitivity:70,scrollSpeed:12,disabled:!!state.runningPlan||state.data?.accounting?.current?.status==='closed',onStart:()=>document.body.classList.add('drag-active'),onEnd:()=>{document.body.classList.remove('drag-active');suppressNodeClickUntil=Date.now()+350},...extra}}
function mountSort(el,options){if(!el)return;Sortable.get(el)?.destroy();new Sortable(el,dragOptions(options))}
function finishDrag(){document.body.classList.remove('drag-active');draggingNode=null;suppressNodeClickUntil=Date.now()+350}
function commitTrackNodeOrder(trackId,ids){const byId=new Map(trackNodes(trackId).map(n=>[n.node_id,n]));if(ids.length!==byId.size||ids.some(id=>!byId.has(id)))return;let i=0;state.draft.nodes=state.draft.nodes.map(n=>n.track_id===trackId?byId.get(ids[i++]):n)}
function setupNodeList(el,trackId,group){
 el.dataset.sortTrack=trackId;
 mountSort(el,{draggable:'[data-sort-node]',filter:'.pf-card-delete',preventOnFilter:false,group:group?{name:group,pull:'clone',put:true}:undefined,
 onStart:e=>{document.body.classList.add('drag-active');draggingNode={id:e.item.dataset.sortNode,track:trackId}},
 onEnd:e=>{finishDrag();if(e.from!==e.to)return;commitTrackNodeOrder(trackId,[...el.querySelectorAll(':scope > [data-sort-node]')].map(n=>n.dataset.sortNode));
 if(el.classList.contains('pf-grid')){el.querySelectorAll('[data-sort-node] .pf-node-number').forEach((n,i)=>n.textContent=String(i+1).padStart(2,'0'));updateSaveState();renderOverview()}else{renderPlan();renderOverview();renderFlow()}toast('节点顺序已调整，正在自动保存')},
 onAdd:e=>{const target=state.draft.tracks.find(t=>t.track_id===trackId),original=state.draft.nodes.find(n=>n.node_id===e.item.dataset.sortNode);if(!target||!original){e.item.remove();return}
 const copy={node_id:crypto.randomUUID().replaceAll('-',''),track_id:trackId,dataset_id:target.dataset_id,name:original.name,detail_sql:original.detail_sql,tolerance:original.tolerance||'0.001',enabled:original.enabled!==false,check_mode:original.check_mode||'zero'};
 if(copy.check_mode==='compare'){copy.compare_op=original.compare_op||'>';copy.compare_value=original.compare_value??'';if(original.report_label)copy.report_label=original.report_label}
 e.item.dataset.sortNode=copy.node_id;state.draft.nodes.push(copy);commitTrackNodeOrder(trackId,[...el.querySelectorAll(':scope > [data-sort-node]')].map(n=>n.dataset.sortNode));
 setTimeout(()=>{finishDrag();renderPlan();renderOverview();renderFlow();toast('节点已复制，请检查目标主表字段')},0)}
 });
}
function destroySortTree(root){if(!root)return;[root,...root.querySelectorAll('*')].forEach(el=>Sortable.get(el)?.destroy())}
const sourceRenderBeforeSort=renderSources;
renderSources=function(){destroySortTree($('source-grid'));sourceRenderBeforeSort();mountSort($('source-grid'),{draggable:'.source-card',handle:'.source-drag-handle',disabled:sourceOrderSaving||state.data?.accounting?.current?.status==='closed',onEnd:async e=>{finishDrag();if(e.oldIndex===e.newIndex)return;const previous=[...state.data.sources],ids=[...$('source-grid').children].map(el=>el.dataset.sourceId);sourceOrderSaving=true;Sortable.get($('source-grid')).option('disabled',true);try{await api('/api/sources/order','POST',{ids});state.data.sources=ids.map(id=>previous.find(s=>s.dataset_id===id));toast('数据源顺序已保存')}catch(err){state.data.sources=previous;notice(err.message)}finally{sourceOrderSaving=false;renderSources()}}})};
const flowRenderBeforeSort=renderFlow;
renderFlow=function(){destroySortTree($('flow-tracks'));flowRenderBeforeSort();const root=$('flow-tracks');root.querySelectorAll(':scope > .dataset-track').forEach(el=>setupNodeList(el.querySelector('.rule-track'),el.dataset.trackId,'result-nodes'));
 mountSort(root,{draggable:'.dataset-track',handle:'.track-drag-handle',onEnd:e=>{finishDrag();if(e.oldIndex===e.newIndex)return;const ids=[...root.children].map(el=>el.dataset.trackId),map=new Map(state.draft.tracks.map(t=>[t.track_id,t]));state.draft.tracks=ids.map(id=>map.get(id));state.draft.nodes=state.draft.tracks.flatMap(t=>trackNodes(t.track_id));renderPlan();renderOverview();renderFlow();toast('流程顺序已调整，正在自动保存')}})
};
const planRenderBeforeSort=renderPlan;
renderPlan=function(){destroySortTree(document.querySelector('.pf-file-fan'));planRenderBeforeSort();const fan=document.querySelector('.pf-file-fan');if(fan)setupNodeList(fan,state.activeTrackId)};
// Overlay grids are attached only when the folder is opened.
new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes){if(node.nodeType!==1||!node.matches('.pf-overlay'))continue;const grid=node.querySelector('.pf-grid');if(grid)setupNodeList(grid,state.activeTrackId)}}).observe(document.body,{childList:true});

const overviewRenderBeforeSort=renderOverview;
renderOverview=function(){destroySortTree($('overview-track'));overviewRenderBeforeSort();$('overview-track').querySelectorAll('.dataset-track').forEach(el=>setupNodeList(el.querySelector('.rule-track'),el.dataset.trackId,'overview-nodes'))};
