// Period operations use saved server state; archives never edit the live draft.
function renderAccountingHeader(){
  const current=state.data?.accounting?.current;if(!current)return;
  $('periods').textContent=current.period+' · '+(current.status==='closed'?'已关账':'处理中');
  document.body.classList.toggle('period-closed',current.status==='closed');
  let button=$('manage-periods');if(!button){button=uiButton('账期管理',openAccounting,'button ghost');button.id='manage-periods';document.querySelector('.header-meta').prepend(button)}
}
async function openAccounting(){
  const dialog=make('dialog','accounting-dialog'),head=make('div','dialog-head'),body=make('div','accounting-body');
  head.append(make('h2','','账期管理'),uiButton('关闭',()=>dialog.close()));dialog.append(head,body);document.body.append(dialog);dialog.showModal();dialog.onclose=()=>dialog.remove();
  let busy=false;
  async function refresh(){
    try{const data=await api('/api/periods');state.data.accounting=data;renderAccountingHeader();paint(data)}catch(e){body.replaceChildren(make('p','notice',e.message))}
  }
  async function action(path,payload){
    if(busy)return;busy=true;body.querySelectorAll('button').forEach(b=>b.disabled=true);
    try{await api('/api/periods/'+path,'POST',payload);await load(true);planUI.baseline=draftValue();renderPlan();renderAccountingHeader();await refresh()}
    catch(e){const error=body.querySelector('.accounting-error');if(error)error.textContent=e.message}
    finally{busy=false;body.querySelectorAll('button').forEach(b=>b.disabled=false)}
  }
  function field(label,type='text'){const wrap=make('label','accounting-field',label),input=make('input');input.type=type;wrap.append(input);return [wrap,input]}
  function paint(data){
    body.replaceChildren();const current=data.current,summary=make('div','accounting-summary');summary.append(make('strong','',current.period),make('span','badge '+(current.status==='closed'?'passed':'pending'),current.status==='closed'?'已关账 · 只读':'处理中'));body.append(summary);
    const error=make('p','accounting-error');error.setAttribute('role','alert');body.append(error);
    if(current.status==='open'){
      const [personLabel,person]=field('业务确认人'),[noteLabel,note]=field('确认说明（存在未通过节点时必填）');person.maxLength=100;note.maxLength=4000;
      const confirm=make('label','accounting-confirm'),check=make('input');check.type='checkbox';confirm.append(check,make('span','','我已核对当前账期的数据、最终方案及检验结果，确认关账。'));
      const close=uiButton('业务确认并关账',()=>{if(dirtyPlan()){error.textContent='方案有未保存修改，请先保存并重新运行。';return}if(!check.checked||!person.value.trim()){error.textContent='请填写确认人并勾选业务确认。';return}action('close',{confirmed_by:person.value,note:note.value,confirmed:true})},'button primary');
      body.append(personLabel,noteLabel,confirm,close);
    }else{
      const [label,input]=field('新账期','month'),[year,mon]=current.period.split('-').map(Number);input.value=mon===12?(year+1)+'-01':year+'-'+String(mon+1).padStart(2,'0');
      body.append(label,make('p','accounting-hint','继承校验方案和标准参考表；检验主表需导入新月份数据。'),uiButton('创建新账期',()=>action('new',{period:input.value}),'button primary'));
    }
    body.append(make('h3','','已关账版本'));
    if(!data.archives.length)body.append(make('p','accounting-hint','暂无归档'));
    data.archives.forEach(item=>{
      const row=make('article','accounting-archive'),title=make('div');title.append(make('strong','',item.period+' · 归档 V'+item.revision),make('p','',item.confirmed_by+' · '+time(item.closed_at)+' · '+statusText(item.result)));
      const actions=make('div','accounting-actions'),download=make('a','button ghost','下载完整归档');download.href='/api/periods/download?id='+item.id;
      const detail=make('div','accounting-detail hidden'),view=uiButton('查看最终版',async()=>{if(!detail.classList.contains('hidden')){detail.classList.add('hidden');view.textContent='查看最终版';return}view.disabled=true;try{const result=await api('/api/periods/archive?id='+item.id);detail.replaceChildren(make('p','',result.confirmation.note||'无补充说明'));const plan=make('details'),runs=make('details');plan.append(make('summary','','最终方案与 SQL'),make('pre','wb-ai-code',JSON.stringify(result.plan,null,2)));runs.append(make('summary','','最终检验结果'),make('pre','wb-ai-code',JSON.stringify(result.run,null,2)));detail.append(plan,runs);detail.classList.remove('hidden');view.textContent='收起'}catch(e){error.textContent=e.message}finally{view.disabled=false}});
      const reopen=uiButton('管理员重开',()=>{
        const auth=make('dialog','wb-dialog'),[passLabel,password]=field('管理员密码','password'),[reasonLabel,reason]=field('重开原因'),msg=make('p','accounting-error'),buttons=make('div','dialog-actions');
        const submit=uiButton('确认重开',async()=>{if(!reason.value.trim()){msg.textContent='请填写重开原因';return}submit.disabled=true;let token='';try{const login=await api('/api/wren/admin/login','POST',{password:password.value});password.value='';token=login.token;await api('/api/periods/reopen','POST',{token,archive_id:item.id,reason:reason.value});auth.close();await load(true);planUI.baseline=draftValue();renderPlan();renderAccountingHeader();await refresh()}catch(e){msg.textContent=e.message}finally{if(token)api('/api/wren/admin/logout','POST',{token}).catch(()=>{});submit.disabled=false}},'button primary');
        buttons.append(uiButton('取消',()=>auth.close()),submit);auth.append(make('h2','','重开 '+item.period),passLabel,reasonLabel,msg,buttons);auth.onclose=()=>auth.remove();document.body.append(auth);auth.showModal();password.focus()
      });reopen.disabled=current.status!=='closed';actions.append(view,download,reopen);row.append(title,actions,detail);body.append(row)
    });
  }
  await refresh()
}
const accountingRenderAll=renderAll;
renderAll=function(){accountingRenderAll();renderAccountingHeader()};
if(state.data)renderAccountingHeader();
