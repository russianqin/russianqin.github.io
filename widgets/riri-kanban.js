<script>
/* --- 看板娘弹窗（与史略页同构：洗牌 + 懒预加载 + 键盘翻页） --- */
(function(){
  var PRELOAD=3;

  var modal=document.getElementById('ririModal');
  var openBtn=document.getElementById('ririKanbanBtn');
  if(!modal||!openBtn){return;}

  var img=document.getElementById('ririModalImg');
  var closeBtn=document.getElementById('ririModalClose');
  var navBox=document.getElementById('ririModalNav');
  var prevBtn=document.getElementById('ririModalPrev');
  var nextBtn=document.getElementById('ririModalNext');
  var countEl=document.getElementById('ririModalCount');
  var shot=openBtn.getAttribute('data-images')||'';
  var pool=shot?shot.split(',').filter(Boolean):[];   /* 图池（固定顺序，别动） */
  var list=[];                                        /* 本次打开的洗牌结果 */
  var idx=0;
  var preloaded={};
  var lastFirst=null;

  /* 图池为空时按钮不响应，避免打开空白弹窗；填入 data-images 后自动生效 */
  if(!pool.length){openBtn.disabled=true;openBtn.style.opacity='.45';return;}

  /* Fisher–Yates 洗牌，返回新数组，不改动 pool */
  function shuffled(src){
    var a=src.slice();
    for(var i=a.length-1;i>0;i--){
      var j=Math.floor(Math.random()*(i+1));
      var t=a[i];a[i]=a[j];a[j]=t;
    }
    return a;
  }
  /* 懒预加载：翻到哪预取到哪 */
  function prime(from,count){
    for(var i=from;i<from+count&&i<list.length;i++){
      if(preloaded[list[i]]){continue;}
      preloaded[list[i]]=1;
      var p=new Image();p.src=list[i];
    }
  }
  function render(){
    if(!list.length){return;}
    img.src=list[idx];
    img.alt='看板娘 '+(idx+1);
    var multi=list.length>1;
    navBox.classList.toggle('riri-modal-nav-show',multi);
    if(multi){
      countEl.textContent=(idx+1)+' / '+list.length;
      prevBtn.disabled=(idx===0);
      nextBtn.disabled=(idx===list.length-1);
    }
    /* 当前张 + 后面 PRELOAD 张 */
    prime(idx,PRELOAD);
  }
  function open(){
    if(!pool.length){return;}
    /* 每次点开都重新洗牌；若首张与上次相同且不止一张，再抽一次，
       避免"随机了但看着没变" */
    var next=shuffled(pool);
    if(pool.length>1&&next[0]===lastFirst){
      next=shuffled(pool);
    }
    lastFirst=next[0];
    list=next;idx=0;render();
    modal.classList.add('riri-modal-open');
    document.body.style.overflow='hidden';
  }
  function close(){
    modal.classList.remove('riri-modal-open');
    document.body.style.overflow='';
  }
  function go(step){
    var n=idx+step;
    if(n<0||n>=list.length){return;}
    idx=n;render();
  }
  openBtn.addEventListener('click',open);
  closeBtn.addEventListener('click',close);
  prevBtn.addEventListener('click',function(e){e.stopPropagation();go(-1);});
  nextBtn.addEventListener('click',function(e){e.stopPropagation();go(1);});
  modal.addEventListener('click',function(e){if(e.target===modal){close();}});
  document.addEventListener('keydown',function(e){
    if(!modal.classList.contains('riri-modal-open')){return;}
    if(e.key==='Escape'){close();}
    else if(e.key==='ArrowLeft'){go(-1);}
    else if(e.key==='ArrowRight'){go(1);}
  });
  /* 首次打开前不预加载任何图，避免首屏白下几百 KB；
     打开时 render() 会按洗牌结果取前 PRELOAD 张。 */
})();
</script>
